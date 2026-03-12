"""BaseProvider protocol + ProviderConfig."""

from __future__ import annotations

from typing import Any, AsyncIterator, Protocol, runtime_checkable

from pydantic import BaseModel

from agent_kit.types import Message, ToolSchema, Turn


class ProviderConfig(BaseModel):
    """Common configuration shared by all providers."""

    api_key: str | None = None
    base_url: str | None = None
    default_model: str = ""
    timeout_s: float = 60.0
    max_retries: int = 3


@runtime_checkable
class BaseProvider(Protocol):
    """
    Protocol that every LLM provider adapter must satisfy.

    Implementing classes do NOT need to inherit from BaseProvider —
    duck typing via @runtime_checkable is enough.
    """

    config: ProviderConfig

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        tools: list[ToolSchema] | None = None,
        system: str | None = None,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> Turn:
        """
        Send messages to the LLM and return a completed Turn.

        Tool calls requested by the model are included in Turn.tool_calls.
        The Turn does NOT include tool results — those are added by AgentLoop.
        """
        ...

    async def stream(
        self,
        messages: list[Message],
        model: str | None = None,
        system: str | None = None,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Yield text chunks as they arrive from the provider."""
        ...

    def name(self) -> str:
        """Human-readable provider name, e.g. 'anthropic', 'openai', 'ollama'."""
        ...
