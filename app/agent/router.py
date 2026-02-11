"""
🧭 Director Router — Classificador leve de instruções.

Usa um modelo barato (gpt-4o-mini) SEM tools para classificar a instrução
do usuário em uma rota:

- payload  → PayloadSpecialist (posição, timing, animação, zoom)
- replay   → ReplaySpecialist (cor, fonte, tamanho, background, matting,
              corte de silêncios, posicionamento de b-rolls)
- impossible → Resposta direta sem chamar LLM specialist

Custo por chamada: ~$0.0005 (gpt-4o-mini, ~500 tokens)
Latência: ~300-500ms (1 chamada, sem tools)
"""

import json
import logging
from typing import Optional

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


# Rotas válidas
VALID_ROUTES = {"payload", "replay", "impossible"}

ROUTER_SYSTEM_PROMPT = """Você é um classificador de instruções para um sistema de edição de vídeo.

Sua ÚNICA tarefa é classificar a instrução em UMA das seguintes rotas:

## payload
Modificações que podem ser feitas DIRETAMENTE no payload final (rápido, ~20s):
- Reposicionar legendas (mover para cima, baixo, esquerda, direita)
- Ajustar timing (quando legendas aparecem/desaparecem)
- Mudar animação (fade-in, slide, bounce)
- Adicionar/remover zoom dinâmico
- Mudar ordem de camadas (zIndex)
- Remover camadas inteiras (matting, backgrounds)
- Ajustar visibilidade de elementos

## replay
Modificações que exigem RE-GERAR assets do pipeline (mais lento, ~35-60s):
- Mudar COR do texto/legendas
- Mudar FONTE do texto
- Mudar TAMANHO do texto
- Mudar estilo de BACKGROUND/cartela
- Mudar SOMBRAS
- Regenerar MOTION GRAPHICS
- Ativar/desativar MATTING (recorte da pessoa)
- Qualquer mudança visual que afete imagens PNG pré-renderizadas
- Ajustar CORTE DE SILÊNCIOS (cortar mais/menos agressivamente, mudar sensibilidade)
- Reposicionar B-ROLLS (mudar posicionamento de b-rolls, adicionar/remover b-rolls, ajustar duração)
- Mudar TÍTULO do vídeo (texto, cor, fonte, posição, timing) — usa step `title_generation`

## impossible
Coisas que NÃO são possíveis:
- Mudar o conteúdo das LEGENDAS (o que está escrito) — requer re-transcrição. ATENÇÃO: mudar o TÍTULO é possível via replay!
- Mudar resolução/fps do vídeo — quebraria layout
- Mudar o vídeo base — requer novo upload
- Pedidos que não fazem sentido no contexto de edição de vídeo

Responda APENAS com um JSON no formato: {"route": "payload"} ou {"route": "replay"} ou {"route": "impossible", "reason": "explicação breve"}

Nenhum outro texto. Apenas o JSON."""


class DirectorRouter:
    """
    Classificador leve que roteia instruções para o specialist correto.

    Usa gpt-4o-mini (ou modelo configurado) SEM tools.
    Uma única chamada, ~300-500ms, ~$0.0005.
    """

    def __init__(self, api_key: str, model: str = "gpt-4o-mini"):
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model

    async def classify(
        self,
        instruction: str,
        context: Optional[dict] = None,
    ) -> dict:
        """
        Classifica uma instrução em uma rota.

        Args:
            instruction: Instrução do usuário (ex: "mude a cor do texto para azul")
            context: Contexto adicional (job_id, template_id, etc.)

        Returns:
            {"route": "payload"|"replay"|"impossible", "reason": "..." (se impossible)}
        """
        user_message = f"Instrução: {instruction}"
        if context:
            if context.get("template_id"):
                user_message += f"\nTemplate: {context['template_id']}"
            if context.get("project_id"):
                user_message += f"\nProjeto: {context['project_id']}"

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.0,
                max_tokens=100,
                response_format={"type": "json_object"},
            )

            content = response.choices[0].message.content or "{}"
            result = json.loads(content)

            route = result.get("route", "payload")
            reason = result.get("reason", "")

            # Validar rota
            if route not in VALID_ROUTES:
                logger.warning(f"⚠️ Router retornou rota inválida: {route}, fallback para 'payload'")
                route = "payload"

            # Log tokens usados
            usage = response.usage
            tokens_in = usage.prompt_tokens if usage else 0
            tokens_out = usage.completion_tokens if usage else 0

            logger.info(
                f"🧭 Router: '{instruction[:60]}...' → {route}"
                f"{f' ({reason})' if reason else ''}"
                f" [{tokens_in}+{tokens_out} tokens]"
            )

            return {
                "route": route,
                "reason": reason,
                "tokens_input": tokens_in,
                "tokens_output": tokens_out,
            }

        except Exception as e:
            logger.error(f"❌ Router falhou: {e}. Fallback para 'payload'.")
            return {
                "route": "payload",
                "reason": f"Router error, fallback: {e}",
                "tokens_input": 0,
                "tokens_output": 0,
            }
