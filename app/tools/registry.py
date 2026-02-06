"""
Tool Registry — Registra e gerencia tools disponíveis para o Director.

Cada tool é uma função async que recebe argumentos e retorna um dict.
As tools são convertidas para o formato OpenAI function calling.
"""

import logging
from typing import Callable, Any

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Registra tools e converte para formato OpenAI."""

    def __init__(self):
        self._tools: dict[str, dict] = {}
        self._handlers: dict[str, Callable] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        handler: Callable,
    ):
        """Registra uma tool."""
        self._tools[name] = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters,
            },
        }
        self._handlers[name] = handler
        logger.debug(f"🔧 Tool registrada: {name}")

    def get_openai_tools(self, allowed_tools: list[str] = None) -> list[dict]:
        """Retorna tools no formato OpenAI function calling."""
        if not allowed_tools:
            return list(self._tools.values())

        return [
            tool for name, tool in self._tools.items()
            if name in allowed_tools
        ]

    async def execute(self, name: str, arguments: dict) -> dict:
        """Executa uma tool por nome."""
        handler = self._handlers.get(name)
        if not handler:
            return {"error": f"Tool '{name}' não encontrada"}

        try:
            result = await handler(**arguments)
            return result
        except Exception as e:
            logger.error(f"❌ Erro na tool {name}: {e}", exc_info=True)
            return {"error": f"{type(e).__name__}: {str(e)}"}

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())
