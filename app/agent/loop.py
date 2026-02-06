"""
🧠 Agent Loop — O coração do LLM Sandbox Director+1

Ciclo: observe → think → act → verify → repeat/stop

O LLM vê o resultado de cada ação no histórico de mensagens.
Se algo der errado, ele corrige sozinho (até o limite de iterações).
Todas as ações são logadas no banco (director_sessions + director_actions).
"""

import json
import time
import logging
from typing import AsyncIterator, Optional

from openai import AsyncOpenAI

from ..config import DirectorConfig
from ..tools.registry import ToolRegistry
from ..tools.observation import register_observation_tools
from ..tools.payload import register_payload_tools
from ..tools.render import register_render_tools
from ..db import session as db
from .prompts import build_system_prompt

logger = logging.getLogger(__name__)


# ═══ Token cost calculation ═══

MODEL_COSTS = {
    # Custo por 1M tokens (USD)
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o-2024-11-20": {"input": 2.50, "output": 10.00},
}


def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Calcula custo em USD de uma chamada LLM."""
    costs = MODEL_COSTS.get(model, MODEL_COSTS["gpt-4o"])
    return (input_tokens * costs["input"] / 1_000_000) + (
        output_tokens * costs["output"] / 1_000_000
    )


class SandboxDirector:
    """
    LLM Sandbox Director+1

    Agente com tools e sandbox para controle do pipeline de vídeo.
    Invocado pelo Chatbot quando operações complexas são necessárias.
    """

    def __init__(self, config: DirectorConfig):
        self.config = config
        self.client = AsyncOpenAI(api_key=config.openai_api_key)
        self.registry = ToolRegistry()

        # Registrar tools
        register_observation_tools(
            self.registry,
            v_api_url=config.v_api_internal_url,
            service_token=config.v_api_service_token,
        )
        register_payload_tools(
            self.registry,
            v_api_url=config.v_api_internal_url,
            service_token=config.v_api_service_token,
        )
        register_render_tools(
            self.registry,
            v_api_url=config.v_api_internal_url,
            service_token=config.v_api_service_token,
            max_rerenders=config.director_max_rerenders,
        )

        logger.info(
            f"🤖 Director inicializado — model={config.director_model}, "
            f"tools={self.registry.tool_names}, "
            f"max_iter={config.director_max_iterations}"
        )

    async def execute(
        self,
        job_id: str,
        instruction: str,
        user_id: Optional[str] = None,
        context: Optional[dict] = None,
    ) -> AsyncIterator[dict]:
        """
        Agent loop: observe → think → act → verify → ...

        Yields eventos conforme o Director avança:
          {"type": "session_created", "session_id": "..."}
          {"type": "tool_call", "iteration": 1, "tool": "list_tracks", ...}
          {"type": "complete", "result": "...", "total_iterations": 5, ...}
          {"type": "error", "result": "...", ...}
        """
        context = context or {}

        # ═══ Criar sessão no banco ═══
        session_id = await db.create_session(
            job_id=job_id,
            user_id=user_id,
            instruction=instruction,
            model=self.config.director_model,
            max_iterations=self.config.director_max_iterations,
            max_sandbox_calls=self.config.director_max_sandbox_calls,
            budget_limit_usd=self.config.director_budget_limit_usd,
        )

        yield {"type": "session_created", "session_id": session_id}

        # ═══ Construir system prompt ═══
        system_prompt = build_system_prompt(
            max_iterations=self.config.director_max_iterations,
            max_sandbox_calls=self.config.director_max_sandbox_calls,
            max_rerenders=self.config.director_max_rerenders,
            budget_limit=self.config.director_budget_limit_usd,
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"Job ID: {job_id}\nInstrução: {instruction}",
            },
        ]

        # Adicionar contexto extra se fornecido
        if context.get("template_id"):
            messages[-1]["content"] += f"\nTemplate: {context['template_id']}"
        if context.get("project_id"):
            messages[-1]["content"] += f"\nProjeto: {context['project_id']}"

        # ═══ Contadores ═══
        total_tool_calls = 0
        total_sandbox_calls = 0
        total_rerenders = 0
        total_tokens_input = 0
        total_tokens_output = 0
        total_cost = 0.0
        sandbox_total_time_ms = 0

        # ═══ Tools no formato OpenAI ═══
        openai_tools = self.registry.get_openai_tools(
            self.config.allowed_tools_list or None
        )

        # ═══════════════════════════════════════════════════
        # AGENT LOOP
        # ═══════════════════════════════════════════════════

        for iteration in range(1, self.config.director_max_iterations + 1):
            logger.info(f"🔄 Iteração {iteration}/{self.config.director_max_iterations}")

            # ── Budget check ──
            if total_cost > self.config.director_budget_limit_usd:
                logger.warning(f"💰 Budget limit atingido: ${total_cost:.4f}")
                await db.complete_session(
                    session_id, "budget_exceeded",
                    error_message=f"Budget limit: ${total_cost:.4f} > ${self.config.director_budget_limit_usd}",
                )
                yield {
                    "type": "error",
                    "status": "budget_exceeded",
                    "result": f"Limite de orçamento atingido (${total_cost:.4f}). Sessão encerrada.",
                    "total_iterations": iteration,
                    "total_cost": total_cost,
                }
                return

            # ── Chamada ao LLM ──
            try:
                response = await self.client.chat.completions.create(
                    model=self.config.director_model,
                    messages=messages,
                    tools=openai_tools if openai_tools else None,
                    tool_choice="auto" if openai_tools else None,
                    temperature=self.config.director_temperature,
                )
            except Exception as e:
                logger.error(f"❌ Erro na chamada LLM: {e}")
                await db.complete_session(session_id, "error", error_message=str(e))
                yield {"type": "error", "status": "error", "result": f"Erro LLM: {e}"}
                return

            # ── Contabilizar tokens ──
            usage = response.usage
            if usage:
                total_tokens_input += usage.prompt_tokens
                total_tokens_output += usage.completion_tokens
                total_cost += calculate_cost(
                    self.config.director_model,
                    usage.prompt_tokens,
                    usage.completion_tokens,
                )

            choice = response.choices[0]
            assistant_message = choice.message

            # Adicionar resposta do assistente ao histórico
            messages.append(assistant_message.model_dump(exclude_none=True))

            # ── CASO 1: Tool calls ──
            if assistant_message.tool_calls:
                for tool_call in assistant_message.tool_calls:
                    tool_name = tool_call.function.name
                    tool_args = json.loads(tool_call.function.arguments)

                    logger.info(f"  🔧 Tool: {tool_name}({json.dumps(tool_args, ensure_ascii=False)[:100]})")

                    # Executar tool
                    start_time = time.time()
                    result = await self.registry.execute(tool_name, tool_args)
                    duration_ms = int((time.time() - start_time) * 1000)

                    total_tool_calls += 1
                    is_success = "error" not in result

                    # Contar re-renders
                    if tool_name == "re_render" and is_success:
                        total_rerenders += 1

                    # Logar ação no banco
                    await db.log_action(
                        session_id=session_id,
                        iteration=iteration,
                        action_type="tool_call",
                        tool_name=tool_name,
                        tool_args=tool_args,
                        tool_result=result,
                        tool_duration_ms=duration_ms,
                        tool_success=is_success,
                        tokens_input=usage.prompt_tokens if usage else 0,
                        tokens_output=usage.completion_tokens if usage else 0,
                        cost_usd=calculate_cost(
                            self.config.director_model,
                            usage.prompt_tokens if usage else 0,
                            usage.completion_tokens if usage else 0,
                        ),
                    )

                    # Resultado volta ao histórico → LLM vê na próxima iteração
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(result, default=str, ensure_ascii=False),
                    })

                    # Emit evento
                    yield {
                        "type": "tool_call",
                        "iteration": iteration,
                        "tool": tool_name,
                        "args": tool_args,
                        "success": is_success,
                        "duration_ms": duration_ms,
                        "cost_so_far": round(total_cost, 6),
                    }

                    logger.info(
                        f"  {'✅' if is_success else '❌'} {tool_name} → "
                        f"{json.dumps(result, ensure_ascii=False)[:200]}"
                    )

            # ── CASO 2: Resposta final (sem tool calls) ──
            else:
                result_text = assistant_message.content or "Concluído sem mensagem."

                logger.info(f"✅ Director concluiu em {iteration} iterações: {result_text[:200]}")

                # Logar resposta final
                await db.log_action(
                    session_id=session_id,
                    iteration=iteration,
                    action_type="llm_response",
                    llm_response_text=result_text,
                    tokens_input=usage.prompt_tokens if usage else 0,
                    tokens_output=usage.completion_tokens if usage else 0,
                    cost_usd=calculate_cost(
                        self.config.director_model,
                        usage.prompt_tokens if usage else 0,
                        usage.completion_tokens if usage else 0,
                    ),
                )

                # Atualizar contadores e completar sessão
                await db.update_session_counters(
                    session_id=session_id,
                    total_iterations=iteration,
                    total_tool_calls=total_tool_calls,
                    total_sandbox_calls=total_sandbox_calls,
                    total_rerenders=total_rerenders,
                    total_tokens_input=total_tokens_input,
                    total_tokens_output=total_tokens_output,
                    total_cost_usd=total_cost,
                    sandbox_total_time_ms=sandbox_total_time_ms,
                    sandbox_total_cost_usd=0,
                )
                await db.complete_session(
                    session_id, "completed", result_summary=result_text
                )

                yield {
                    "type": "complete",
                    "status": "completed",
                    "result": result_text,
                    "session_id": session_id,
                    "total_iterations": iteration,
                    "total_tool_calls": total_tool_calls,
                    "total_cost": round(total_cost, 6),
                }
                return

        # ── Limite de iterações ──
        logger.warning(f"⚠️ Max iterações atingido ({self.config.director_max_iterations})")
        await db.update_session_counters(
            session_id=session_id,
            total_iterations=self.config.director_max_iterations,
            total_tool_calls=total_tool_calls,
            total_sandbox_calls=total_sandbox_calls,
            total_rerenders=total_rerenders,
            total_tokens_input=total_tokens_input,
            total_tokens_output=total_tokens_output,
            total_cost_usd=total_cost,
            sandbox_total_time_ms=sandbox_total_time_ms,
            sandbox_total_cost_usd=0,
        )
        await db.complete_session(
            session_id, "error",
            error_message=f"Max iterations reached ({self.config.director_max_iterations})",
        )

        yield {
            "type": "error",
            "status": "max_iterations",
            "result": f"Limite de iterações atingido ({self.config.director_max_iterations})",
            "total_iterations": self.config.director_max_iterations,
            "total_cost": round(total_cost, 6),
        }
