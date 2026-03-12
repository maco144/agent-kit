"""OpenAI / OpenAI-compatible provider adapter."""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

from agent_kit.exceptions import ProviderError
from agent_kit.providers.base import ProviderConfig
from agent_kit.types import CostSummary, Message, ToolCall, ToolSchema, Turn

try:
    import openai
except ImportError as e:
    raise ImportError(
        "The 'openai' package is required for OpenAIProvider. "
        "Install it with: pip install agent-kit[openai]"
    ) from e

_COST_TABLE: dict[str, tuple[float, float]] = {
    "gpt-4o":        (2.50, 10.00),
    "gpt-4-turbo":   (10.00, 30.00),
    "gpt-4":         (30.00, 60.00),
    "gpt-3.5-turbo": (0.50, 1.50),
    "o1":            (15.00, 60.00),
    "o1-mini":       (3.00, 12.00),
}

_DEFAULT_MODEL = "gpt-4o"


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    for prefix, (in_rate, out_rate) in _COST_TABLE.items():
        if model.startswith(prefix):
            return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000
    return 0.0


def _to_openai_tools(schemas: list[ToolSchema]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": s.name,
                "description": s.description,
                "parameters": s.parameters,
            },
        }
        for s in schemas
    ]


def _messages_to_openai(messages: list[Message]) -> list[dict[str, Any]]:
    result = []
    for msg in messages:
        if msg.role == "tool":
            result.append(
                {
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id,
                    "content": msg.content,
                }
            )
        else:
            result.append({"role": msg.role, "content": msg.content})
    return result


class OpenAIProvider:
    """
    LLM provider adapter for OpenAI (and OpenAI-compatible APIs).

    Usage::

        provider = OpenAIProvider()                        # uses OPENAI_API_KEY env var
        provider = OpenAIProvider(api_key="sk-...")
        provider = OpenAIProvider(                         # OpenAI-compatible endpoint
            base_url="http://localhost:11434/v1",
            api_key="ollama",
            default_model="llama3.2",
        )
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

        self._client = openai.AsyncOpenAI(**kwargs)

    def name(self) -> str:
        return "openai"

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
        converted = _messages_to_openai(messages)

        # Prepend system message if given and not already in messages
        if system and not any(m["role"] == "system" for m in converted):
            converted = [{"role": "system", "content": system}] + converted

        call_kwargs: dict[str, Any] = {
            "model": resolved_model,
            "messages": converted,
            "max_tokens": max_tokens,
            **kwargs,
        }
        if tools:
            call_kwargs["tools"] = _to_openai_tools(tools)
            call_kwargs["tool_choice"] = "auto"

        t0 = time.monotonic()
        try:
            response = await self._client.chat.completions.create(**call_kwargs)
        except openai.APIError as exc:
            raise ProviderError(f"OpenAI API error: {exc}") from exc

        duration_ms = int((time.monotonic() - t0) * 1000)
        choice = response.choices[0]
        msg = choice.message

        tool_calls: list[ToolCall] = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append(
                    ToolCall(
                        tool_name=tc.function.name,
                        arguments=json.loads(tc.function.arguments or "{}"),
                        call_id=tc.id,
                    )
                )

        usage = response.usage
        input_tokens = usage.prompt_tokens if usage else 0
        output_tokens = usage.completion_tokens if usage else 0
        cost_usd = _estimate_cost(resolved_model, input_tokens, output_tokens)
        cost = CostSummary(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cost_usd=cost_usd,
            model=resolved_model,
        )

        assistant_msg = Message(role="assistant", content=msg.content or "")
        return Turn(
            messages_in=messages,
            message_out=assistant_msg,
            tool_calls=tool_calls,
            cost=cost,
            duration_ms=duration_ms,
        )

    async def stream(
        self,
        messages: list[Message],
        model: str | None = None,
        system: str | None = None,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        resolved_model = model or self.config.default_model
        converted = _messages_to_openai(messages)

        if system and not any(m["role"] == "system" for m in converted):
            converted = [{"role": "system", "content": system}] + converted

        try:
            async with await self._client.chat.completions.create(
                model=resolved_model,
                messages=converted,
                max_tokens=max_tokens,
                stream=True,
                **kwargs,
            ) as stream:
                async for chunk in stream:
                    delta = chunk.choices[0].delta
                    if delta.content:
                        yield delta.content
        except openai.APIError as exc:
            raise ProviderError(f"OpenAI stream error: {exc}") from exc
