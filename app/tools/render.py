"""
Tools de Render — Re-renderização de vídeo.

Chama o endpoint de re-render do v-api.
"""

import httpx
import logging

from .registry import ToolRegistry

logger = logging.getLogger(__name__)


def register_render_tools(
    registry: ToolRegistry,
    v_api_url: str,
    service_token: str,
    max_rerenders: int = 2,
):
    """Registra tools de render no registry."""

    headers = {
        "Authorization": f"Bearer {service_token}",
        "apikey": service_token,
        "Content-Type": "application/json",
    }

    _rerender_count = {"value": 0}

    # ═══ re_render ═══
    async def re_render(job_id: str) -> dict:
        """
        Re-renderiza o vídeo com o payload atual (modificado).
        
        O payload já deve ter sido modificado via modify_payload.
        Este endpoint envia o payload salvo para v-editor-python.
        """
        _rerender_count["value"] += 1
        if _rerender_count["value"] > max_rerenders:
            return {
                "error": f"Limite de re-renders atingido ({max_rerenders}). "
                "Não é possível re-renderizar mais nesta sessão.",
                "limit_reached": True,
            }

        async with httpx.AsyncClient(timeout=120) as client:
            # Primeiro buscar o payload atual
            payload_resp = await client.get(
                f"{v_api_url}/api/debug/render-payload/{job_id}",
                headers=headers,
            )
            if payload_resp.status_code != 200:
                return {"error": f"Erro ao buscar payload para re-render: {payload_resp.status_code}"}

            payload_data = payload_resp.json()

            # Enviar para re-render
            resp = await client.post(
                f"{v_api_url}/api/debug/re-render",
                headers=headers,
                json={
                    "job_id": job_id,
                    "editor": "python",
                    "payload": payload_data.get("payload", payload_data),
                },
                timeout=120,
            )

            if resp.status_code != 200:
                return {"error": f"Erro no re-render: {resp.status_code} - {resp.text}"}

            result = resp.json()
            return {
                "success": True,
                "status": "rendering",
                "message": "Vídeo enviado para re-renderização. Aguarde ~20-30 segundos.",
                "render_count": _rerender_count["value"],
                "remaining_rerenders": max_rerenders - _rerender_count["value"],
            }

    registry.register(
        name="re_render",
        description=(
            "Re-renderiza o vídeo com o payload atual (após modificações). "
            "O payload deve ter sido modificado via modify_payload primeiro. "
            "O render demora ~20-30 segundos. "
            f"Limite: {max_rerenders} re-renders por sessão. "
            "IMPORTANTE: Sempre valide o payload antes de re-renderizar."
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
        handler=re_render,
    )

    logger.info(f"🔧 {1} tool de render registrada (max re-renders: {max_rerenders})")
