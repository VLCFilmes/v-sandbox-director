"""
Database session e conexão — Audit logging para o Director.

Usa asyncpg diretamente (sem ORM) para simplicidade e performance.
"""

import asyncpg
import logging
import json
import uuid
from datetime import datetime, timezone
from typing import Optional, Any

logger = logging.getLogger(__name__)

# Pool de conexões
_pool: Optional[asyncpg.Pool] = None


async def init_db(database_url: str):
    """Inicializa pool de conexões e cria tabelas se não existirem."""
    global _pool
    _pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)
    logger.info("✅ Database pool criado")

    # Criar tabelas automaticamente
    async with _pool.acquire() as conn:
        await conn.execute(CREATE_TABLES_SQL)
    logger.info("✅ Tabelas director_sessions e director_actions verificadas/criadas")


async def close_db():
    """Fecha pool de conexões."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool não inicializado. Chame init_db() primeiro.")
    return _pool


# ═══════════════════════════════════════════════════════════
# CRUD — Sessões do Director
# ═══════════════════════════════════════════════════════════

async def create_session(
    job_id: str,
    user_id: Optional[str],
    instruction: str,
    model: str,
    max_iterations: int,
    max_sandbox_calls: int,
    budget_limit_usd: float,
) -> str:
    """Cria uma nova sessão do Director. Retorna session_id."""
    pool = get_pool()
    session_id = str(uuid.uuid4())

    await pool.execute(
        """
        INSERT INTO director_sessions (
            id, job_id, user_id, instruction, status,
            model, max_iterations, max_sandbox_calls, budget_limit_usd,
            started_at
        ) VALUES ($1, $2, $3, $4, 'running', $5, $6, $7, $8, $9)
        """,
        session_id,
        job_id,
        user_id,
        instruction,
        model,
        max_iterations,
        max_sandbox_calls,
        budget_limit_usd,
        datetime.now(timezone.utc),
    )

    logger.info(f"📝 Sessão Director criada: {session_id} (job: {job_id})")
    return session_id


async def update_session_counters(
    session_id: str,
    total_iterations: int,
    total_tool_calls: int,
    total_sandbox_calls: int,
    total_rerenders: int,
    total_tokens_input: int,
    total_tokens_output: int,
    total_cost_usd: float,
    sandbox_total_time_ms: int,
    sandbox_total_cost_usd: float,
):
    """Atualiza contadores da sessão (chamado a cada iteração)."""
    pool = get_pool()
    await pool.execute(
        """
        UPDATE director_sessions SET
            total_iterations = $2,
            total_tool_calls = $3,
            total_sandbox_calls = $4,
            total_rerenders = $5,
            total_tokens_input = $6,
            total_tokens_output = $7,
            total_cost_usd = $8,
            sandbox_total_time_ms = $9,
            sandbox_total_cost_usd = $10
        WHERE id = $1
        """,
        session_id,
        total_iterations,
        total_tool_calls,
        total_sandbox_calls,
        total_rerenders,
        total_tokens_input,
        total_tokens_output,
        total_cost_usd,
        sandbox_total_time_ms,
        sandbox_total_cost_usd,
    )


async def complete_session(
    session_id: str,
    status: str,
    result_summary: Optional[str] = None,
    error_message: Optional[str] = None,
):
    """Marca sessão como completa ou com erro."""
    pool = get_pool()
    started = await pool.fetchval(
        "SELECT started_at FROM director_sessions WHERE id = $1", session_id
    )
    now = datetime.now(timezone.utc)
    duration_ms = int((now - started).total_seconds() * 1000) if started else 0

    await pool.execute(
        """
        UPDATE director_sessions SET
            status = $2,
            result_summary = $3,
            error_message = $4,
            completed_at = $5,
            duration_ms = $6
        WHERE id = $1
        """,
        session_id,
        status,
        result_summary,
        error_message,
        now,
        duration_ms,
    )
    logger.info(f"✅ Sessão {session_id} → {status} ({duration_ms}ms)")


# ═══════════════════════════════════════════════════════════
# CRUD — Ações do Director (cada step do loop)
# ═══════════════════════════════════════════════════════════

async def log_action(
    session_id: str,
    iteration: int,
    action_type: str,
    # Tool call
    tool_name: Optional[str] = None,
    tool_args: Optional[dict] = None,
    tool_result: Optional[Any] = None,
    tool_duration_ms: Optional[int] = None,
    tool_success: Optional[bool] = None,
    # Sandbox
    sandbox_code: Optional[str] = None,
    sandbox_success: Optional[bool] = None,
    sandbox_error: Optional[str] = None,
    sandbox_duration_ms: Optional[int] = None,
    sandbox_stdout: Optional[str] = None,
    sandbox_cost_usd: Optional[float] = None,
    # LLM
    tokens_input: Optional[int] = None,
    tokens_output: Optional[int] = None,
    cost_usd: Optional[float] = None,
    llm_response_text: Optional[str] = None,
):
    """Loga uma ação do Director (tool call, sandbox, ou resposta LLM)."""
    pool = get_pool()
    action_id = str(uuid.uuid4())

    # Truncar tool_result se muito grande (max 10KB)
    tool_result_json = None
    if tool_result is not None:
        serialized = json.dumps(tool_result, default=str)
        if len(serialized) > 10240:
            tool_result_json = json.dumps({
                "_truncated": True,
                "_original_size": len(serialized),
                "_preview": serialized[:2000]
            })
        else:
            tool_result_json = serialized

    await pool.execute(
        """
        INSERT INTO director_actions (
            id, session_id, iteration, action_type,
            tool_name, tool_args, tool_result, tool_duration_ms, tool_success,
            sandbox_code, sandbox_success, sandbox_error, sandbox_duration_ms,
            sandbox_stdout, sandbox_cost_usd,
            tokens_input, tokens_output, cost_usd, llm_response_text,
            created_at
        ) VALUES (
            $1, $2, $3, $4,
            $5, $6, $7, $8, $9,
            $10, $11, $12, $13, $14, $15,
            $16, $17, $18, $19,
            $20
        )
        """,
        action_id,
        session_id,
        iteration,
        action_type,
        tool_name,
        json.dumps(tool_args) if tool_args else None,
        tool_result_json,
        tool_duration_ms,
        tool_success,
        sandbox_code,
        sandbox_success,
        sandbox_error,
        sandbox_duration_ms,
        sandbox_stdout,
        sandbox_cost_usd,
        tokens_input,
        tokens_output,
        cost_usd,
        llm_response_text,
        datetime.now(timezone.utc),
    )


# ═══════════════════════════════════════════════════════════
# DDL — Criação de tabelas
# ═══════════════════════════════════════════════════════════

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS director_sessions (
    id TEXT PRIMARY KEY,
    job_id TEXT,
    user_id TEXT,
    instruction TEXT NOT NULL,
    status VARCHAR(20) DEFAULT 'running',

    model VARCHAR(50) NOT NULL,
    max_iterations INT NOT NULL,
    max_sandbox_calls INT NOT NULL,
    budget_limit_usd DECIMAL(10,4),

    total_iterations INT DEFAULT 0,
    total_tool_calls INT DEFAULT 0,
    total_sandbox_calls INT DEFAULT 0,
    total_rerenders INT DEFAULT 0,

    total_tokens_input INT DEFAULT 0,
    total_tokens_output INT DEFAULT 0,
    total_cost_usd DECIMAL(10,6) DEFAULT 0,

    sandbox_total_time_ms INT DEFAULT 0,
    sandbox_total_cost_usd DECIMAL(10,8) DEFAULT 0,

    result_summary TEXT,
    error_message TEXT,

    started_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    completed_at TIMESTAMP WITH TIME ZONE,
    duration_ms INT
);

CREATE TABLE IF NOT EXISTS director_actions (
    id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES director_sessions(id) ON DELETE CASCADE,
    iteration INT NOT NULL,

    action_type VARCHAR(20) NOT NULL,

    tool_name VARCHAR(100),
    tool_args TEXT,
    tool_result TEXT,
    tool_duration_ms INT,
    tool_success BOOLEAN,

    sandbox_code TEXT,
    sandbox_success BOOLEAN,
    sandbox_error TEXT,
    sandbox_duration_ms INT,
    sandbox_stdout TEXT,
    sandbox_cost_usd DECIMAL(10,8),

    tokens_input INT,
    tokens_output INT,
    cost_usd DECIMAL(10,6),
    llm_response_text TEXT,

    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_director_sessions_job ON director_sessions(job_id);
CREATE INDEX IF NOT EXISTS idx_director_sessions_user ON director_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_director_sessions_status ON director_sessions(status);
CREATE INDEX IF NOT EXISTS idx_director_actions_session ON director_actions(session_id);
"""
