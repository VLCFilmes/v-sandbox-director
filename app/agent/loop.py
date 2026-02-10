"""
🧠 Agent Loop — O coração do LLM Sandbox Director+1

v3.10.0: Arquitetura Router + Specialists

SmartDirector:
  1. Router classifica instrução (4o-mini, ~$0.0005)
  2. Specialist executa (4o-mini, tools focados)

Specialists:
  - PayloadSpecialist: posição, timing, animação, zoom (6 tools)
  - ReplaySpecialist: cor, fonte, tamanho, bg, matting (4 tools)

Fallback: SandboxDirector legado (todas as 9 tools, prompt unificado)
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
from ..tools.pipeline_replay import register_pipeline_replay_tools
from ..db import session as db
from .prompts import (
    build_system_prompt,
    build_payload_specialist_prompt,
    build_replay_specialist_prompt,
)
from .router import DirectorRouter

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
    costs = MODEL_COSTS.get(model, MODEL_COSTS["gpt-4o-mini"])
    return (input_tokens * costs["input"] / 1_000_000) + (
        output_tokens * costs["output"] / 1_000_000
    )


# ═══ Tool Groups ═══

PAYLOAD_TOOLS = ["list_tracks", "get_track_items", "get_job_status",
                 "modify_payload", "validate_payload", "re_render"]

REPLAY_TOOLS = ["get_job_status",
                "list_pipeline_checkpoints", "get_step_payload", "replay_from_step"]

ALL_TOOLS = PAYLOAD_TOOLS + [t for t in REPLAY_TOOLS if t not in PAYLOAD_TOOLS]


def _build_full_registry(config: DirectorConfig) -> ToolRegistry:
    """Constrói registry com TODAS as tools."""
    registry = ToolRegistry()
    register_observation_tools(
        registry, v_api_url=config.v_api_internal_url,
        service_token=config.v_api_service_token,
    )
    register_payload_tools(
        registry, v_api_url=config.v_api_internal_url,
        service_token=config.v_api_service_token,
    )
    register_render_tools(
        registry, v_api_url=config.v_api_internal_url,
        service_token=config.v_api_service_token,
        max_rerenders=config.director_max_rerenders,
    )
    register_pipeline_replay_tools(
        registry, v_api_url=config.v_api_internal_url,
        service_token=config.v_api_service_token,
        max_replays=config.director_max_rerenders,
    )
    return registry


class SandboxDirector:
    """
    Specialist Director — executa tools em loop para uma rota específica.

    Pode ser usado diretamente (legacy) ou via SmartDirector (Router pattern).
    """

    def __init__(
        self,
        config: DirectorConfig,
        allowed_tools: list[str] = None,
        system_prompt_override: str = None,
    ):
        self.config = config
        self.client = AsyncOpenAI(api_key=config.openai_api_key)
        self.system_prompt_override = system_prompt_override

        # Construir registry completo e filtrar se necessário
        self.registry = _build_full_registry(config)
        self.allowed_tools = allowed_tools  # None = todas

        tool_names = allowed_tools or self.registry.tool_names
        logger.info(
            f"🤖 Specialist inicializado — model={config.director_model}, "
            f"tools={tool_names}"
        )

    async def execute(
        self,
        job_id: str,
        instruction: str,
        user_id: Optional[str] = None,
        context: Optional[dict] = None,
        route: Optional[str] = None,
    ) -> AsyncIterator[dict]:
        """
        Agent loop: observe → think → act → verify → ...

        Yields eventos conforme o Director avança.
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
        if self.system_prompt_override:
            system_prompt = self.system_prompt_override
        else:
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

        # Adicionar contexto extra
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

        # 🆕 v4.4.2: Anti-alucinação + circuit breaker
        consecutive_failures = 0
        MAX_CONSECUTIVE_FAILURES = 3
        # Tools "críticas" = as que efetivamente fazem alterações
        CRITICAL_TOOLS = {"replay_from_step", "re_render", "modify_payload"}
        critical_tool_results = []  # Lista de (tool_name, success, error_msg)

        # ═══ Tools no formato OpenAI (filtradas) ═══
        openai_tools = self.registry.get_openai_tools(
            self.allowed_tools or self.config.allowed_tools_list or None
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
                    if tool_name in ("re_render", "replay_from_step") and is_success:
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

                    # Resultado volta ao histórico
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

                    # 🆕 v4.4.2: Rastrear resultados de tools críticas
                    if tool_name in CRITICAL_TOOLS:
                        error_msg = result.get("error", "") if not is_success else ""
                        critical_tool_results.append((tool_name, is_success, error_msg))

                    # 🆕 v4.4.2: Circuit breaker — falhas consecutivas
                    if is_success:
                        consecutive_failures = 0
                    else:
                        consecutive_failures += 1
                        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                            error_summary = (
                                f"Múltiplas falhas consecutivas ({consecutive_failures}). "
                                f"Última falha: {tool_name} → {result.get('error', 'erro desconhecido')}"
                            )
                            logger.warning(f"🛑 Circuit breaker ativado: {error_summary}")
                            await db.complete_session(
                                session_id, "error",
                                error_message=f"Circuit breaker: {error_summary}",
                            )
                            yield {
                                "type": "error",
                                "status": "circuit_breaker",
                                "result": (
                                    f"Não foi possível completar a alteração. "
                                    f"O sistema encontrou {consecutive_failures} erros seguidos. "
                                    f"Detalhes: {error_summary}"
                                ),
                                "total_iterations": iteration,
                                "total_cost": round(total_cost, 6),
                            }
                            return

            # ── CASO 2: Resposta final ──
            else:
                result_text = assistant_message.content or "Concluído sem mensagem."

                # 🆕 v4.4.2: Anti-alucinação — verificar se tools críticas tiveram sucesso
                # Se a LLM diz que fez algo mas NENHUMA tool crítica teve sucesso,
                # substituir a resposta por uma mensagem honesta de falha.
                if critical_tool_results:
                    any_critical_success = any(
                        success for _, success, _ in critical_tool_results
                    )
                    if not any_critical_success:
                        failed_details = "; ".join(
                            f"{name}: {err}" for name, _, err in critical_tool_results if err
                        )
                        original_text = result_text
                        result_text = (
                            f"Não foi possível realizar a alteração solicitada. "
                            f"As operações falharam: {failed_details}. "
                            f"Isso pode acontecer quando o tipo de modificação não é suportado "
                            f"neste contexto ou quando checkpoints necessários não estão disponíveis."
                        )
                        logger.warning(
                            f"🛡️ [ANTI-HALLUCINATION] Resposta original substituída. "
                            f"Original: '{original_text[:200]}' → Corrigida: '{result_text[:200]}'"
                        )

                logger.info(f"✅ Specialist concluiu em {iteration} iterações: {result_text[:200]}")

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

                # 🆕 v4.4.2: Determinar status real com base no anti-alucinação
                all_critical_failed = (
                    critical_tool_results
                    and not any(s for _, s, _ in critical_tool_results)
                )
                final_status = "completed_with_errors" if all_critical_failed else "completed"

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
                    session_id, final_status, result_summary=result_text
                )

                yield {
                    "type": "complete",
                    "status": final_status,
                    "result": result_text,
                    "session_id": session_id,
                    "total_iterations": iteration,
                    "total_tool_calls": total_tool_calls,
                    "total_cost": round(total_cost, 6),
                    "route": route,
                    # 🆕 v4.4.2: Incluir detalhes de falha para o frontend
                    **({"had_critical_failures": True} if all_critical_failed else {}),
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


# ═══════════════════════════════════════════════════════════════
# SmartDirector — Router + Specialists
# ═══════════════════════════════════════════════════════════════

class SmartDirector:
    """
    🧭 Director com Router pattern.

    1. Router classifica instrução (~$0.0005, ~300ms)
    2. Specialist correto executa (tools focados, prompt menor)

    Benefícios:
    - Cada specialist tem ~50% menos tokens no prompt
    - 4o-mini performa melhor com contexto focado
    - Custo total: ~$0.0015/sessão (vs $0.017 com 4o)
    """

    def __init__(self, config: DirectorConfig):
        self.config = config

        # Router (usa router_model, default: gpt-4o-mini)
        router_model = getattr(config, 'router_model', 'gpt-4o-mini')
        self.router = DirectorRouter(
            api_key=config.openai_api_key,
            model=router_model,
        )

        # Payload Specialist
        self.payload_specialist = SandboxDirector(
            config,
            allowed_tools=PAYLOAD_TOOLS,
            system_prompt_override=build_payload_specialist_prompt(
                max_iterations=config.director_max_iterations,
                max_rerenders=config.director_max_rerenders,
                budget_limit=config.director_budget_limit_usd,
            ),
        )

        # Replay Specialist
        self.replay_specialist = SandboxDirector(
            config,
            allowed_tools=REPLAY_TOOLS,
            system_prompt_override=build_replay_specialist_prompt(
                max_iterations=config.director_max_iterations,
                max_replays=config.director_max_rerenders,
                budget_limit=config.director_budget_limit_usd,
            ),
        )

        logger.info(
            f"🧭 SmartDirector inicializado — "
            f"router={router_model}, specialist={config.director_model}, "
            f"payload_tools={len(PAYLOAD_TOOLS)}, replay_tools={len(REPLAY_TOOLS)}"
        )

    async def execute(
        self,
        job_id: str,
        instruction: str,
        user_id: Optional[str] = None,
        context: Optional[dict] = None,
    ) -> AsyncIterator[dict]:
        """
        1. Router classifica
        2. Specialist executa
        """
        context = context or {}

        # ═══ Step 1: Router ═══
        route_result = await self.router.classify(instruction, context)
        route = route_result["route"]

        yield {
            "type": "routed",
            "route": route,
            "reason": route_result.get("reason", ""),
            "router_tokens": route_result.get("tokens_input", 0) + route_result.get("tokens_output", 0),
        }

        # ═══ Step 2: Dispatch ═══

        if route == "impossible":
            reason = route_result.get("reason", "Modificação não suportada")
            yield {
                "type": "complete",
                "status": "completed",
                "result": f"Não foi possível atender: {reason}",
                "total_iterations": 0,
                "total_cost": calculate_cost(
                    getattr(self.config, 'router_model', 'gpt-4o-mini'),
                    route_result.get("tokens_input", 0),
                    route_result.get("tokens_output", 0),
                ),
                "route": route,
            }
            return

        # Selecionar specialist
        if route == "replay":
            specialist = self.replay_specialist
            logger.info(f"🔄 [SMART] Roteando para ReplaySpecialist")
        else:
            specialist = self.payload_specialist
            logger.info(f"📝 [SMART] Roteando para PayloadSpecialist")

        # Executar specialist (yield all events)
        async for event in specialist.execute(
            job_id=job_id,
            instruction=instruction,
            user_id=user_id,
            context=context,
            route=route,
        ):
            # Enriquecer eventos com info do router
            if event.get("type") == "complete":
                router_cost = calculate_cost(
                    getattr(self.config, 'router_model', 'gpt-4o-mini'),
                    route_result.get("tokens_input", 0),
                    route_result.get("tokens_output", 0),
                )
                event["total_cost"] = round(
                    event.get("total_cost", 0) + router_cost, 6
                )
                event["route"] = route
            yield event
