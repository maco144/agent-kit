"""BaseProvider protocol + ProviderConfig."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, AsyncIterator, Protocol, runtime_checkable

from pydantic import BaseModel

from agent_kit.types import Message, ToolSchema, Turn

if TYPE_CHECKING:
    from agent_kit.output import OutputSpec


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

    Providers that constrain answers natively set ``supports_structured_output = True`` and honour
    ``output_schema``. AgentLoop only passes ``output_schema`` to such providers; others receive the
    schema as system prompt instructions instead. Providers whose native constraint prevents tool calls
    also set ``structured_output_with_tools = False``; runs with tools then use the prompt instead.
    """

    config: ProviderConfig

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        tools: list[ToolSchema] | None = None,
        system: str | None = None,
        max_tokens: int = 4096,
        output_schema: OutputSpec[Any] | None = None,
        **kwargs: Any,
    ) -> Turn:
        """
        Send messages to the LLM and return a completed Turn.

        Tool calls requested by the model are included in Turn.tool_calls.
        The Turn does NOT include tool results — those are added by AgentLoop.
        """
        ...

    def stream(
        self,
        messages: list[Message],
        model: str | None = None,
        tools: list[ToolSchema] | None = None,
        system: str | None = None,
        max_tokens: int = 4096,
        output_schema: OutputSpec[Any] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str | Turn]:
        """
        Yield text chunks as they arrive, then optionally one final Turn.

        The final Turn carries tool calls and cost, exactly as complete() would
        return. Providers that yield only text still work; AgentLoop builds a
        text-only Turn from the chunks.
        """
        ...

    def name(self) -> str:
        """Human-readable provider name, e.g. 'anthropic', 'openai', 'ollama'."""
        ...
