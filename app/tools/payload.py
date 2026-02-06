"""
Tools de Payload — Modificação e validação do payload de vídeo.

Todas chamam o v-api via HTTP interno.
"""

import httpx
import logging

from .registry import ToolRegistry

logger = logging.getLogger(__name__)


def register_payload_tools(
    registry: ToolRegistry,
    v_api_url: str,
    service_token: str,
):
    """Registra tools de payload no registry."""

    headers = {
        "Authorization": f"Bearer {service_token}",
        "apikey": service_token,
        "Content-Type": "application/json",
    }

    # ═══ modify_payload ═══
    async def modify_payload(
        job_id: str,
        modifications: dict,
    ) -> dict:
        """
        Aplica modificações ao payload salvo de um job.

        modifications é um dict com caminhos e valores:
        {
            "tracks.subtitles[*].animation.entrance.type": "slide_up",
            "tracks.user_logo_layer": [{"id": "logo_0", ...}],
            "global.font_size": 78
        }
        """
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{v_api_url}/api/video/payload/modify",
                headers=headers,
                json={
                    "job_id": job_id,
                    "modifications": modifications,
                },
            )

            if resp.status_code != 200:
                return {"error": f"Erro ao modificar payload: {resp.status_code} - {resp.text}"}

            return resp.json()

    registry.register(
        name="modify_payload",
        description=(
            "Aplica modificações ao payload de vídeo salvo. "
            "Aceita um dict de modificações com caminhos de campos. "
            "Exemplos de modifications:\n"
            '  {"tracks.subtitles[*].animation.entrance.type": "slide_up"} → muda animação de TODAS as subtitles\n'
            '  {"tracks.user_logo_layer": [{...}]} → define conteúdo da track de logo\n'
            '  {"global.font_size": 78} → muda fonte global\n'
            "O operador [*] aplica a TODOS os items da track."
        ),
        parameters={
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "ID do job",
                },
                "modifications": {
                    "type": "object",
                    "description": (
                        "Dict com caminhos e valores a modificar. "
                        "Use tracks.<nome>[*].<campo> para aplicar a todos os items de uma track."
                    ),
                },
            },
            "required": ["job_id", "modifications"],
        },
        handler=modify_payload,
    )

    # ═══ validate_payload ═══
    async def validate_payload(job_id: str) -> dict:
        """Valida integridade do payload de um job."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{v_api_url}/api/video/payload/validate",
                headers=headers,
                json={"job_id": job_id},
            )

            if resp.status_code != 200:
                return {"error": f"Erro ao validar: {resp.status_code} - {resp.text}"}

            return resp.json()

    registry.register(
        name="validate_payload",
        description=(
            "Valida a integridade do payload de vídeo de um job. "
            "Verifica: tracks obrigatórias, campos requeridos, timings, referências de assets. "
            "Retorna {valid: true/false, warnings: [...], errors: [...]}. "
            "SEMPRE use antes de re-renderizar."
        ),
        parameters={
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "ID do job",
                },
            },
            "required": ["job_id"],
        },
        handler=validate_payload,
    )

    logger.info(f"🔧 {2} tools de payload registradas")
