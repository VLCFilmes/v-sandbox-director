"""
Tools de Observação — Leitura do estado do pipeline/payload.

Todas chamam o v-api via HTTP interno.
"""

import httpx
import logging
from typing import Optional

from .registry import ToolRegistry

logger = logging.getLogger(__name__)


def register_observation_tools(
    registry: ToolRegistry,
    v_api_url: str,
    service_token: str,
):
    """Registra tools de observação no registry."""

    headers = {
        "Authorization": f"Bearer {service_token}",
        "apikey": service_token,
        "Content-Type": "application/json",
    }

    # ═══ list_tracks ═══
    async def list_tracks(job_id: str) -> dict:
        """
        Retorna RESUMO das tracks do payload de um job.
        NÃO retorna o payload completo — apenas nome, count e time_range de cada track.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{v_api_url}/api/video/payload/tracks/{job_id}",
                headers=headers,
            )

            if resp.status_code != 200:
                return {"error": f"Erro ao buscar tracks: {resp.status_code} - {resp.text}"}

            return resp.json()

    registry.register(
        name="list_tracks",
        description=(
            "Retorna um RESUMO das tracks do payload do vídeo. "
            "Cada track mostra: nome, quantidade de items, range de tempo, e 1 item de exemplo. "
            "Use SEMPRE como primeiro passo para entender o estado do vídeo. "
            "NÃO retorna o payload completo."
        ),
        parameters={
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "ID do job de processamento de vídeo",
                },
            },
            "required": ["job_id"],
        },
        handler=list_tracks,
    )

    # ═══ get_track_items ═══
    async def get_track_items(
        job_id: str,
        track_name: str,
        limit: Optional[int] = 5,
        offset: Optional[int] = 0,
    ) -> dict:
        """Retorna items de uma track específica (com paginação)."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{v_api_url}/api/video/payload/tracks/{job_id}",
                headers=headers,
                params={
                    "track_name": track_name,
                    "limit": limit or 5,
                    "offset": offset or 0,
                },
            )

            if resp.status_code != 200:
                return {"error": f"Erro ao buscar track {track_name}: {resp.status_code}"}

            return resp.json()

    registry.register(
        name="get_track_items",
        description=(
            "Retorna items de uma track específica do payload. "
            "Use após list_tracks quando precisar ver detalhes de uma track. "
            "Suporta paginação com limit e offset."
        ),
        parameters={
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "ID do job",
                },
                "track_name": {
                    "type": "string",
                    "description": "Nome da track (ex: subtitles, person_overlay, bg_full_screen)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Máximo de items a retornar (default: 5)",
                    "default": 5,
                },
                "offset": {
                    "type": "integer",
                    "description": "Pular N items iniciais (default: 0)",
                    "default": 0,
                },
            },
            "required": ["job_id", "track_name"],
        },
        handler=get_track_items,
    )

    # ═══ get_job_status ═══
    async def get_job_status(job_id: str) -> dict:
        """Retorna status atual do job de processamento."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{v_api_url}/api/video/job/{job_id}",
                headers=headers,
            )

            if resp.status_code != 200:
                return {"error": f"Erro ao buscar status: {resp.status_code}"}

            data = resp.json()
            # Retornar só campos relevantes para o Director
            return {
                "job_id": data.get("job_id"),
                "status": data.get("status"),
                "project_id": data.get("project_id"),
                "template_id": data.get("template_id"),
                "current_step": data.get("current_step"),
                "phase2_video_url": data.get("phase2_video_url"),
                "created_at": data.get("created_at"),
            }

    registry.register(
        name="get_job_status",
        description=(
            "Retorna o status atual de um job de processamento de vídeo. "
            "Inclui: status, step atual, template usado, URL do vídeo final."
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
        handler=get_job_status,
    )

    logger.info(f"🔧 {3} tools de observação registradas")
