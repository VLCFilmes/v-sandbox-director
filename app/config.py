"""
Configuração do LLM Sandbox Director+1

Todas as configurações são lidas de variáveis de ambiente.
Ver .env.example para lista completa.
"""

from pydantic_settings import BaseSettings
from typing import Optional


class DirectorConfig(BaseSettings):
    """Configurações do Director — todas via ENV."""

    # ═══ LLM ═══
    openai_api_key: str
    director_model: str = "gpt-4o-mini"
    director_temperature: float = 0.1

    # ═══ Router ═══
    router_model: str = "gpt-4o-mini"          # Modelo do classificador (barato)
    router_enabled: bool = True                 # False = fallback para Director unificado

    # ═══ Controles do Agent Loop ═══
    director_max_iterations: int = 10
    director_max_sandbox_calls: int = 3
    director_max_rerenders: int = 2
    director_budget_limit_usd: float = 0.50
    director_sandbox_timeout: int = 30

    # ═══ v-api (interno) ═══
    v_api_internal_url: str = "http://v-api:5000"
    v_api_service_token: str = ""

    # ═══ v-llm-directors (Level 0) ═══
    v_llm_directors_url: str = "http://v-llm-directors:5025"

    # ═══ Modal Sandbox (Fase 4) ═══
    modal_sandbox_url: str = ""

    # ═══ Database ═══
    database_url: str

    # ═══ Redis ═══
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password: str = ""

    # ═══ Segurança ═══
    director_allow_internet_sandbox: bool = False
    director_allowed_tools: str = "all"
    director_log_level: str = "full"

    @property
    def redis_url(self) -> str:
        if self.redis_password:
            return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}/0"
        return f"redis://{self.redis_host}:{self.redis_port}/0"

    @property
    def allowed_tools_list(self) -> list[str]:
        if self.director_allowed_tools == "all":
            return []  # Vazio = todas permitidas
        return [t.strip() for t in self.director_allowed_tools.split(",")]

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


# Singleton
_config: Optional[DirectorConfig] = None


def get_config() -> DirectorConfig:
    global _config
    if _config is None:
        _config = DirectorConfig()
    return _config
