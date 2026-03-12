"""LLM provider adapters."""

from agent_kit.providers.anthropic import AnthropicProvider
from agent_kit.providers.base import BaseProvider, ProviderConfig

__all__ = [
    "AnthropicProvider",
    "BaseProvider",
    "ProviderConfig",
]

# Optional providers — imported lazily to avoid hard dependency errors
def get_openai_provider() -> type:
    from agent_kit.providers.openai import OpenAIProvider
    return OpenAIProvider

def get_ollama_provider() -> type:
    from agent_kit.providers.ollama import OllamaProvider
    return OllamaProvider
