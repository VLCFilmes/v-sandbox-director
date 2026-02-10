"""
🔄 Tools de Pipeline Replay — Re-execução parcial do pipeline.

v3.10.0: Permite ao Director re-executar partes do pipeline
com modificações no estado (ex: mudar cor do texto → re-gerar PNGs).

Ferramentas:
- list_pipeline_checkpoints: Listar checkpoints salvos de um job
- get_step_payload: Inspecionar estado completo de um step
- replay_from_step: Re-executar pipeline a partir de um step com modificações

Todas chamam o v-api via HTTP interno.
"""

import httpx
import logging
from typing import Optional

from .registry import ToolRegistry

logger = logging.getLogger(__name__)


# Mapa de step → tipo de modificação (para descrições informativas)
STEP_MODIFICATION_MAP = {
    "detect_silence": "Corte de silêncios (sensibilidade, threshold, modo de corte)",
    "silence_cut": "Corte de silêncios (re-executa a partir do corte)",
    "generate_pngs": "Cor, fonte, tamanho e estilo do texto",
    "add_shadows": "Sombras e efeitos visuais",
    "apply_animations": "Animações de entrada/saída",
    "calculate_positions": "Posição das legendas no canvas",
    "generate_backgrounds": "Backgrounds e cartelas (cores, estilo)",
    "motion_graphics": "Motion graphics (Manim)",
    "matting": "Matting (pessoa recortada on/off)",
    "video_clipper": "Posicionamento de b-rolls (re-gera EDL via LLM semântica)",
    "subtitle_pipeline": "Layout final de tracks e composição",
    "render": "Re-renderização com payload atual",
}


def register_pipeline_replay_tools(
    registry: ToolRegistry,
    v_api_url: str,
    service_token: str,
    max_replays: int = 2,
):
    """Registra tools de Pipeline Replay no registry."""

    headers = {
        "Authorization": f"Bearer {service_token}",
        "apikey": service_token,
        "Content-Type": "application/json",
    }

    _replay_count = {"value": 0}

    # ═══ list_pipeline_checkpoints ═══
    async def list_pipeline_checkpoints(job_id: str) -> dict:
        """
        Lista checkpoints salvos de um job — mostra quais steps têm
        dados salvos para replay.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{v_api_url}/api/video/job/{job_id}/checkpoints",
                headers=headers,
                params={"include_parent": "true"},
            )

            if resp.status_code != 200:
                return {"error": f"Erro ao buscar checkpoints: {resp.status_code} - {resp.text}"}

            data = resp.json()
            checkpoints = data.get("checkpoints", [])

            # Enriquecer com informação de modificação
            for cp in checkpoints:
                step = cp.get("step_name", "")
                cp["modifiable"] = step in STEP_MODIFICATION_MAP
                cp["modification_type"] = STEP_MODIFICATION_MAP.get(step, "")

            # Se há root_job_id, incluir para que o LLM use no replay_from_step
            root_job_id = data.get("root_job_id")
            
            result = {
                "job_id": job_id,
                "checkpoint_count": len(checkpoints),
                "checkpoints": checkpoints,
                "replay_steps_available": [
                    cp["step_name"] for cp in checkpoints
                    if cp.get("modifiable")
                ],
            }
            
            if root_job_id:
                result["root_job_id"] = root_job_id
                # Identificar steps que vêm do parent (para hints)
                parent_steps = [
                    cp["step_name"] for cp in checkpoints
                    if cp.get("from_parent")
                ]
                if parent_steps:
                    result["parent_only_steps"] = parent_steps
                    result["hint"] = (
                        f"Este job é um replay. Steps {parent_steps} vêm do job original "
                        f"({root_job_id[:8]}...). Para replay desses steps, use "
                        f"root_job_id='{root_job_id}' como job_id no replay_from_step."
                    )
            
            return result

    registry.register(
        name="list_pipeline_checkpoints",
        description=(
            "Lista checkpoints do pipeline salvos para um job. "
            "Cada checkpoint é o estado completo após um step executar. "
            "Use ANTES de replay_from_step para saber quais steps têm dados. "
            "Mostra: step_name, duration_ms, completed_steps, e quais steps "
            "permitem replay com modificações."
        ),
        parameters={
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "ID do job cujos checkpoints serão listados",
                },
            },
            "required": ["job_id"],
        },
        handler=list_pipeline_checkpoints,
    )

    # ═══ get_step_payload ═══
    async def get_step_payload(
        job_id: str,
        step_name: str,
    ) -> dict:
        """
        Retorna o estado completo do pipeline após um step executar.
        Usado para inspecionar campos antes de replay.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{v_api_url}/api/video/job/{job_id}/checkpoints/{step_name}",
                headers=headers,
                params={"include_parent": "true"},
            )

            if resp.status_code == 404:
                return {
                    "error": f"Checkpoint não encontrado para step '{step_name}'. "
                    "Use list_pipeline_checkpoints para ver steps disponíveis.",
                    "not_found": True,
                }

            if resp.status_code != 200:
                return {"error": f"Erro ao buscar checkpoint: {resp.status_code} - {resp.text}"}

            data = resp.json()
            state = data.get("state", {})

            # Retornar campos mais relevantes para o Director
            # (evitar devolver o state inteiro que pode ser enorme)
            relevant_fields = {
                "text_styles": state.get("text_styles"),
                "template_id": state.get("template_id"),
                "template_config": _summarize_template_config(state.get("template_config")),
                "video_width": state.get("video_width"),
                "video_height": state.get("video_height"),
                "total_duration_ms": state.get("total_duration_ms"),
                "phrase_groups_count": len(state.get("phrase_groups") or []),
                "completed_steps": state.get("completed_steps", []),
                "has_png_results": state.get("png_results") is not None,
                "has_shadow_results": state.get("shadow_results") is not None,
                "has_animation_results": state.get("animation_results") is not None,
                "has_positioning_results": state.get("positioning_results") is not None,
                "has_background_results": state.get("background_results") is not None,
            }

            # Se step é generate_pngs ou anterior, extrair modifiable_fields
            # com paths EXATOS para a LLM usar diretamente
            modifiable_fields = {}
            if step_name in ("generate_pngs", "classify", "load_template"):
                ts = state.get("text_styles", {})
                default_ts = ts.get("default") or {}
                
                # Extrair campos com paths exatos para replay
                fc = default_ts.get("font_config") or {}
                hl = default_ts.get("highlight") or {}
                bg = default_ts.get("background") or {}
                shadow = default_ts.get("shadow") or {}
                
                modifiable_fields = {
                    "text_styles.default.font_config.font_color.value": _safe_value(fc, "font_color"),
                    "text_styles.default.font_config.font_family.value": _safe_value(fc, "font_family"),
                    "text_styles.default.font_config.font_size.value": _safe_value(fc, "font_size"),
                    "text_styles.default.font_config.weight": fc.get("weight"),
                    "text_styles.default.font_config.uppercase": fc.get("uppercase"),
                    "text_styles.default.highlight.color.value": _safe_value(hl, "color"),
                    "text_styles.default.highlight.style.value": _safe_value(hl, "style"),
                    "text_styles.default.highlight.enabled.value": _safe_value(hl, "enabled"),
                    "text_styles.default.background.color.value": _safe_value(bg, "color"),
                    "text_styles.default.background.enabled": bg.get("enabled"),
                    "text_styles.default.shadow.enabled.value": _safe_value(shadow, "enabled"),
                }
                # Borders
                borders = default_ts.get("borders") or []
                if borders:
                    modifiable_fields["text_styles.default.borders[0].color_rgb"] = borders[0].get("color_rgb")
                    modifiable_fields["text_styles.default.borders[0].thickness"] = borders[0].get("thickness")

            # ═══ Silence cutting fields ═══
            elif step_name in ("detect_silence", "silence_cut"):
                opts = state.get("options") or {}
                modifiable_fields = {
                    "options.min_silence_duration": opts.get("min_silence_duration", 0.5),
                    "options.threshold_offset": opts.get("threshold_offset", 3),
                    "options.silence_threshold": opts.get("silence_threshold"),
                    "options.min_speech_duration": opts.get("min_speech_duration", 0.4),
                    "options.cut_mode": opts.get("cut_mode", "all_silences"),
                    "options.trim_start": opts.get("trim_start"),
                    "options.trim_end": opts.get("trim_end"),
                }
                # Adicionar estatísticas de silêncio se disponíveis
                silence_detection = state.get("silence_detection")
                if silence_detection:
                    silence_segs = (
                        silence_detection if isinstance(silence_detection, list)
                        else silence_detection.get("segments", [])
                    )
                    total_silence = sum(
                        (s.get("end", 0) - s.get("start", 0))
                        for s in silence_segs
                    )
                    relevant_fields["silence_stats"] = {
                        "segments_detected": len(silence_segs),
                        "total_silence_seconds": round(total_silence, 2),
                    }
                cut_timestamps = state.get("cut_timestamps")
                if cut_timestamps:
                    cut_list = cut_timestamps if isinstance(cut_timestamps, list) else []
                    relevant_fields["cut_stats"] = {
                        "segments_after_cut": len(cut_list),
                    }

            # ═══ Video Clipper fields ═══
            elif step_name == "video_clipper":
                video_clipper_track = state.get("video_clipper_track")
                modifiable_fields = {
                    "_info": (
                        "O Video Clipper re-gera o EDL inteiro via LLM semântica. "
                        "Para reposicionar b-rolls, faça replay com modifications vazio: {}. "
                        "O cache do EDL é limpo automaticamente."
                    ),
                }
                if video_clipper_track:
                    relevant_fields["video_clipper_stats"] = {
                        "b_roll_placements": len(video_clipper_track),
                        "placements_summary": [
                            {
                                "src": p.get("src", "")[-40:] if p.get("src") else "N/A",
                                "start_time_ms": p.get("start_time"),
                                "end_time_ms": p.get("end_time"),
                                "duration_ms": (
                                    (p.get("end_time") or 0) - (p.get("start_time") or 0)
                                ),
                            }
                            for p in video_clipper_track[:10]  # max 10 para não poluir
                        ],
                    }

            # Gerar hint baseado no tipo
            if modifiable_fields and step_name in ("detect_silence", "silence_cut"):
                hint = (
                    "Use os paths EXATOS de modifiable_fields no replay_from_step. "
                    "Para cortar mais agressivamente: diminua min_silence_duration e threshold_offset. "
                    "Para deixar mais respiro: aumente esses valores."
                )
            elif modifiable_fields and step_name == "video_clipper":
                hint = (
                    "O Video Clipper regenera o EDL inteiro via LLM. "
                    "Para reposicionar b-rolls, faça replay_from_step com modifications={}. "
                    "O step re-analisa a transcrição e os b-rolls disponíveis."
                )
            elif modifiable_fields:
                hint = (
                    "Use os paths EXATOS de modifiable_fields no replay_from_step. "
                    "Cores são arrays [R,G,B,A]. Campos com .value devem ter APENAS o valor, não o objeto inteiro."
                )
            else:
                hint = f"Step '{step_name}' não é um alvo comum de replay."

            return {
                "job_id": job_id,
                "step_name": step_name,
                "found": True,
                "state_summary": relevant_fields,
                "modifiable_fields": modifiable_fields if modifiable_fields else None,
                "modification_type": STEP_MODIFICATION_MAP.get(step_name, ""),
                "hint": hint,
            }

    registry.register(
        name="get_step_payload",
        description=(
            "Retorna o estado do pipeline após um step específico executar. "
            "Use para INSPECIONAR campos antes de decidir quais modificações "
            "fazer no replay. Mostra: text_styles, template_config, silence_options, "
            "video_clipper_stats, contagens, etc. "
            "Para silêncio use step 'detect_silence'. Para b-rolls use step 'video_clipper'. "
            "SEMPRE use após list_pipeline_checkpoints para validar que o step existe."
        ),
        parameters={
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "ID do job",
                },
                "step_name": {
                    "type": "string",
                    "description": (
                        "Nome do step para inspecionar. "
                        "Ex: 'classify' (antes de generate_pngs), "
                        "'generate_pngs', 'add_shadows', "
                        "'detect_silence' (silêncio), 'video_clipper' (b-rolls), etc."
                    ),
                },
            },
            "required": ["job_id", "step_name"],
        },
        handler=get_step_payload,
    )

    # ═══ replay_from_step ═══
    async def replay_from_step(
        job_id: str,
        step_name: str,
        modifications: dict = None,
    ) -> dict:
        """
        Re-executa o pipeline a partir de um step com modificações.
        Cria um novo job e enfileira para processamento.
        """
        if modifications is None:
            modifications = {}

        _replay_count["value"] += 1
        if _replay_count["value"] > max_replays:
            return {
                "error": f"Limite de replays atingido ({max_replays}). "
                "Não é possível fazer mais replays nesta sessão.",
                "limit_reached": True,
            }

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{v_api_url}/api/video/job/{job_id}/replay-from/{step_name}",
                headers=headers,
                json={"modifications": modifications},
            )

            if resp.status_code == 400:
                error_data = resp.json()
                return {
                    "error": error_data.get("error", "Erro de validação"),
                    "success": False,
                }

            if resp.status_code not in (200, 202):
                return {"error": f"Erro no replay: {resp.status_code} - {resp.text}"}

            result = resp.json()
            return {
                "success": True,
                "new_job_id": result.get("new_job_id"),
                "original_job_id": job_id,
                "replaying_from": step_name,
                "steps_to_run": result.get("steps_to_run", []),
                "estimated_time_seconds": result.get("estimated_time_seconds", 30),
                "modifications_applied": result.get("modifications_applied", 0),
                "replay_count": _replay_count["value"],
                "remaining_replays": max_replays - _replay_count["value"],
                "message": (
                    f"Pipeline replay iniciado a partir de '{step_name}'. "
                    f"Novo job: {result.get('new_job_id', 'N/A')[:8]}... "
                    f"Estimativa: ~{result.get('estimated_time_seconds', 30)}s."
                ),
            }

    registry.register(
        name="replay_from_step",
        description=(
            "Re-executa o pipeline a partir de um step com modificações no estado. "
            "CRIA UM NOVO JOB — o vídeo será re-processado do step alvo até o render. "
            "Use quando a modificação exige re-gerar assets (cor, fonte, tamanho do texto, "
            "backgrounds, sombras), ajustar corte de silêncios (detect_silence), ou "
            "reposicionar b-rolls (video_clipper). "
            f"Limite: {max_replays} replays por sessão. "
            "ATENÇÃO: Replay é mais custoso que modify_payload. "
            "Prefira modify_payload para posição, timing, animação, zoom. "
            "Use replay APENAS quando a modificação exige re-gerar PNGs/assets ou re-processar steps. "
            "SEMPRE inspecione com get_step_payload antes de fazer replay."
        ),
        parameters={
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "ID do job original (que tem checkpoints salvos)",
                },
                "step_name": {
                    "type": "string",
                    "description": (
                        "Step a partir do qual re-executar. Exemplos:\n"
                        "- 'detect_silence': ajustar corte de silêncios (sensibilidade, threshold)\n"
                        "- 'generate_pngs': mudar cor/fonte/tamanho do texto\n"
                        "- 'add_shadows': mudar sombras\n"
                        "- 'calculate_positions': mudar posição das legendas\n"
                        "- 'generate_backgrounds': mudar backgrounds/cartelas\n"
                        "- 'motion_graphics': re-gerar motion graphics\n"
                        "- 'matting': ativar/desativar matting\n"
                        "- 'video_clipper': reposicionar b-rolls (regenera EDL via LLM)"
                    ),
                },
                "modifications": {
                    "type": "object",
                    "description": (
                        "Modificações em formato dot-notation. Cores são arrays RGBA [R,G,B,A]. Exemplos:\n"
                        '{"text_styles.default.font_config.font_color.value": [0, 0, 255, 255]}\n'
                        '{"text_styles.default.highlight.color.value": [0, 255, 0, 255]}\n'
                        '{"text_styles.default.font_config.font_size.value": 48}\n'
                        '{"text_styles.default.font_config.font_family.value": "Poppins"}\n'
                        '{"options.min_silence_duration": 0.3, "options.threshold_offset": 1}  (silêncio mais agressivo)\n'
                        '{"options.min_silence_duration": 0.8, "options.threshold_offset": 5}  (mais respiro)\n'
                        '{}  (video_clipper — regenera EDL via LLM, modifications vazio)'
                    ),
                },
            },
            "required": ["job_id", "step_name"],
        },
        handler=replay_from_step,
    )

    logger.info(f"🔧 {3} tools de Pipeline Replay registradas (max replays: {max_replays})")


def _safe_value(parent: dict, key: str):
    """Extract .value from a {value, sidecar_id} field, or None."""
    field = parent.get(key)
    if isinstance(field, dict) and "value" in field:
        return field["value"]
    return field


def _summarize_template_config(tc: dict) -> Optional[dict]:
    """Resumo do template_config (evitar payload enorme)."""
    if not tc:
        return None
    return {
        "template_id": tc.get("template_id") or tc.get("id"),
        "template_name": tc.get("name"),
        "has_text_styles": "text_styles" in tc.get("template-mode", tc.get("template_mode", {})),
        "has_animation_config": "animation_config" in tc.get("template-mode", tc.get("template_mode", {})),
        "has_shadow_config": "shadow_config" in tc.get("template-mode", tc.get("template_mode", {})),
    }
