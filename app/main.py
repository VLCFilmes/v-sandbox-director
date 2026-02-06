"""
🎬 LLM Sandbox Director+1 — FastAPI Application

Endpoints:
  POST /execute       → Executa o Director para um job
  GET  /health        → Health check
  GET  /sessions      → Lista sessões recentes (admin)
  GET  /sessions/{id} → Detalhes de uma sessão (admin)
"""

import logging
import json
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .config import get_config
from .db.session import init_db, close_db
from .agent.loop import SandboxDirector

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)


# ═══ Lifecycle ═══

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup e shutdown da aplicação."""
    config = get_config()
    logger.info("🚀 LLM Sandbox Director+1 iniciando...")
    logger.info(f"   Modelo: {config.director_model}")
    logger.info(f"   Max iterações: {config.director_max_iterations}")
    logger.info(f"   Budget limit: ${config.director_budget_limit_usd}")
    logger.info(f"   v-api URL: {config.v_api_internal_url}")

    # Inicializar DB
    await init_db(config.database_url)
    logger.info("✅ Director pronto")

    yield

    # Shutdown
    await close_db()
    logger.info("👋 Director encerrado")


app = FastAPI(
    title="LLM Sandbox Director+1",
    description="Agente inteligente com tools e sandbox para controle do pipeline de vídeo",
    version="0.1.0",
    lifespan=lifespan,
)


# ═══ Models ═══

class ExecuteRequest(BaseModel):
    """Request para executar o Director."""
    job_id: str
    instruction: str
    user_id: Optional[str] = None
    context: Optional[dict] = None  # Contexto adicional (ex: template_id, preferences)


class ExecuteResponse(BaseModel):
    """Response final do Director."""
    session_id: str
    status: str
    result: Optional[str] = None
    total_iterations: int = 0
    total_cost_usd: float = 0.0
    actions: list = []


# ═══ Endpoints ═══

@app.get("/health")
async def health():
    """Health check simples."""
    config = get_config()
    return {
        "status": "ok",
        "service": "v-sandbox-director",
        "version": "0.1.0",
        "model": config.director_model,
        "max_iterations": config.director_max_iterations,
    }


@app.post("/execute", response_model=ExecuteResponse)
async def execute_director(request: ExecuteRequest):
    """
    Executa o Director para um job específico.

    O Director:
    1. Lê o payload do job (via v-api)
    2. Analisa a instrução
    3. Executa tools/sandbox em loop até resolver
    4. Retorna resultado

    Todas as ações são logadas no banco (director_sessions + director_actions).
    """
    config = get_config()
    director = SandboxDirector(config)

    actions = []
    final_result = None
    session_id = None

    try:
        async for event in director.execute(
            job_id=request.job_id,
            instruction=request.instruction,
            user_id=request.user_id,
            context=request.context or {},
        ):
            event_type = event.get("type")

            if event_type == "session_created":
                session_id = event["session_id"]

            elif event_type == "tool_call":
                actions.append({
                    "type": "tool_call",
                    "iteration": event.get("iteration"),
                    "tool": event.get("tool"),
                    "success": event.get("success", True),
                })

            elif event_type == "sandbox":
                actions.append({
                    "type": "sandbox",
                    "iteration": event.get("iteration"),
                    "success": event.get("success"),
                })

            elif event_type == "complete":
                final_result = event

            elif event_type == "error":
                final_result = event

        return ExecuteResponse(
            session_id=session_id or "unknown",
            status=final_result.get("status", "completed") if final_result else "error",
            result=final_result.get("result") if final_result else "No result",
            total_iterations=final_result.get("total_iterations", 0) if final_result else 0,
            total_cost_usd=final_result.get("total_cost", 0.0) if final_result else 0.0,
            actions=actions,
        )

    except Exception as e:
        logger.error(f"❌ Director execute error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/execute/stream")
async def execute_director_stream(request: ExecuteRequest):
    """
    Executa o Director com streaming SSE.

    Cada evento é enviado como SSE conforme o Director avança.
    Útil para o frontend mostrar progresso em tempo real.
    """
    config = get_config()
    director = SandboxDirector(config)

    async def event_generator():
        try:
            async for event in director.execute(
                job_id=request.job_id,
                instruction=request.instruction,
                user_id=request.user_id,
                context=request.context or {},
            ):
                yield f"data: {json.dumps(event, default=str)}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'result': str(e)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/sessions")
async def list_sessions(limit: int = 20, offset: int = 0):
    """Lista sessões recentes do Director (admin/debug)."""
    from .db.session import get_pool

    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT id, job_id, user_id, instruction, status,
               total_iterations, total_tool_calls, total_sandbox_calls,
               total_cost_usd, duration_ms, started_at, completed_at
        FROM director_sessions
        ORDER BY started_at DESC
        LIMIT $1 OFFSET $2
        """,
        limit,
        offset,
    )

    return {
        "sessions": [dict(row) for row in rows],
        "total": await pool.fetchval("SELECT COUNT(*) FROM director_sessions"),
    }


@app.get("/sessions/{session_id}")
async def get_session_detail(session_id: str):
    """Detalhes completos de uma sessão (admin/debug)."""
    from .db.session import get_pool

    pool = get_pool()

    session = await pool.fetchrow(
        "SELECT * FROM director_sessions WHERE id = $1", session_id
    )
    if not session:
        raise HTTPException(status_code=404, detail="Sessão não encontrada")

    actions = await pool.fetch(
        """
        SELECT * FROM director_actions
        WHERE session_id = $1
        ORDER BY iteration, created_at
        """,
        session_id,
    )

    return {
        "session": dict(session),
        "actions": [dict(a) for a in actions],
    }
