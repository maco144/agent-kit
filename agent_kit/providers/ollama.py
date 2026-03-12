"""Ollama local model provider (OpenAI-compatible API)."""

from __future__ import annotations

from agent_kit.providers.base import ProviderConfig

# Ollama exposes an OpenAI-compatible /v1 endpoint, so we reuse OpenAIProvider.
# This module exists so users can write:
#   from agent_kit.providers import OllamaProvider
# and get sensible defaults (local base_url, no API key required).

try:
    from agent_kit.providers.openai import OpenAIProvider
except ImportError as e:
    raise ImportError(
        "OllamaProvider requires the 'openai' package. "
        "Install it with: pip install agent-kit[openai]"
    ) from e

_DEFAULT_BASE_URL = "http://localhost:11434/v1"
_DEFAULT_MODEL = "llama3.2"


class OllamaProvider(OpenAIProvider):
    """
    Provider adapter for Ollama (local models).

    Ollama exposes an OpenAI-compatible API, so this is a thin subclass
    with defaults wired for local use.

    Usage::

        provider = OllamaProvider()                              # llama3.2 at localhost
        provider = OllamaProvider(default_model="mistral")
        provider = OllamaProvider(base_url="http://gpu-box:11434/v1")
    """

    def __init__(
        self,
        default_model: str = _DEFAULT_MODEL,
        base_url: str = _DEFAULT_BASE_URL,
        timeout_s: float = 120.0,  # local models can be slower
        max_retries: int = 2,
    ) -> None:
        super().__init__(
            api_key="ollama",  # Ollama ignores the key but openai client requires one
            default_model=default_model,
            base_url=base_url,
            timeout_s=timeout_s,
            max_retries=max_retries,
        )

    def name(self) -> str:
        return "ollama"
