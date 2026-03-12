"""Anthropic Claude provider adapter."""

from __future__ import annotations

import time
from typing import Any, AsyncIterator

from agent_kit.exceptions import ProviderError
from agent_kit.providers.base import ProviderConfig
from agent_kit.types import CostSummary, Message, ToolCall, ToolSchema, Turn

try:
    import anthropic
except ImportError as e:
    raise ImportError(
        "The 'anthropic' package is required. Install it with: pip install anthropic"
    ) from e


# Approximate USD cost per million tokens (input/output) by model family.
# These are advisory — actual billing comes from the Anthropic dashboard.
_COST_TABLE: dict[str, tuple[float, float]] = {
    "claude-opus-4":     (15.00, 75.00),
    "claude-sonnet-4":   (3.00,  15.00),
    "claude-haiku-4":    (0.80,  4.00),
    "claude-3-5-sonnet": (3.00,  15.00),
    "claude-3-5-haiku":  (0.80,  4.00),
    "claude-3-opus":     (15.00, 75.00),
    "claude-3-sonnet":   (3.00,  15.00),
    "claude-3-haiku":    (0.25,  1.25),
}

_DEFAULT_MODEL = "claude-sonnet-4-6"


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    for prefix, (in_rate, out_rate) in _COST_TABLE.items():
        if model.startswith(prefix):
            return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000
    return 0.0


def _to_anthropic_tools(schemas: list[ToolSchema]) -> list[dict[str, Any]]:
    return [
        {
            "name": s.name,
            "description": s.description,
            "input_schema": s.parameters,
        }
        for s in schemas
    ]


def _messages_to_anthropic(
    messages: list[Message],
) -> tuple[str | None, list[dict[str, Any]]]:
    """
    Split off the system message and convert the rest to Anthropic's format.

    Returns (system_text | None, anthropic_messages).
    """
    system_text: str | None = None
    result: list[dict[str, Any]] = []

    for msg in messages:
        if msg.role == "system":
            system_text = msg.content
        elif msg.role == "tool":
            # Tool result — append as a user message with tool_result content block
            result.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg.tool_call_id,
                            "content": msg.content,
                        }
                    ],
                }
            )
        else:
            result.append({"role": msg.role, "content": msg.content})

    return system_text, result


class AnthropicProvider:
    """
    LLM provider adapter for Anthropic Claude.

    Usage::

        provider = AnthropicProvider()                        # uses ANTHROPIC_API_KEY env var
        provider = AnthropicProvider(api_key="sk-ant-...")   # explicit key
        provider = AnthropicProvider(default_model="claude-3-haiku-20240307")
    """

    def __init__(
        self,
        api_key: str | None = None,
        default_model: str = _DEFAULT_MODEL,
        timeout_s: float = 60.0,
        max_retries: int = 3,
        base_url: str | None = None,
    ) -> None:
        self.config = ProviderConfig(
            api_key=api_key,
            default_model=default_model,
            timeout_s=timeout_s,
            max_retries=max_retries,
            base_url=base_url,
        )
        kwargs: dict[str, Any] = {
            "timeout": timeout_s,
            "max_retries": max_retries,
        }
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url

        self._client = anthropic.AsyncAnthropic(**kwargs)

    def name(self) -> str:
        return "anthropic"

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        tools: list[ToolSchema] | None = None,
        system: str | None = None,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> Turn:
        resolved_model = model or self.config.default_model
        sys_from_messages, converted = _messages_to_anthropic(messages)
        resolved_system = system or sys_from_messages

        call_kwargs: dict[str, Any] = {
            "model": resolved_model,
            "messages": converted,
            "max_tokens": max_tokens,
            **kwargs,
        }
        if resolved_system:
            call_kwargs["system"] = resolved_system
        if tools:
            call_kwargs["tools"] = _to_anthropic_tools(tools)

        t0 = time.monotonic()
        try:
            response = await self._client.messages.create(**call_kwargs)
        except anthropic.APIError as exc:
            raise ProviderError(f"Anthropic API error: {exc}") from exc

        duration_ms = int((time.monotonic() - t0) * 1000)

        # Parse text content
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        tool_name=block.name,
                        arguments=block.input,
                        call_id=block.id,
                    )
                )

        # Cost accounting
        usage = response.usage
        cost_usd = _estimate_cost(resolved_model, usage.input_tokens, usage.output_tokens)
        cost = CostSummary(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.input_tokens + usage.output_tokens,
            cost_usd=cost_usd,
            model=resolved_model,
        )

        assistant_msg = Message(role="assistant", content=" ".join(text_parts))
        turn = Turn(
            messages_in=messages,
            message_out=assistant_msg,
            tool_calls=tool_calls,
            cost=cost,
            duration_ms=duration_ms,
        )
        return turn

    async def stream(
        self,
        messages: list[Message],
        model: str | None = None,
        system: str | None = None,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        resolved_model = model or self.config.default_model
        sys_from_messages, converted = _messages_to_anthropic(messages)
        resolved_system = system or sys_from_messages

        call_kwargs: dict[str, Any] = {
            "model": resolved_model,
            "messages": converted,
            "max_tokens": max_tokens,
            **kwargs,
        }
        if resolved_system:
            call_kwargs["system"] = resolved_system

        try:
            async with self._client.messages.stream(**call_kwargs) as stream:
                async for text in stream.text_stream:
                    yield text
        except anthropic.APIError as exc:
            raise ProviderError(f"Anthropic stream error: {exc}") from exc
