"""OpenAI / OpenAI-compatible provider adapter."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, AsyncIterator

from agent_kit.exceptions import ProviderError, ResponseTruncatedError
from agent_kit.providers.base import ProviderConfig
from agent_kit.providers.pricing import cached_input_rate, lookup_rates
from agent_kit.types import CostSummary, Message, RequestOptions, ToolCall, ToolSchema, Turn

if TYPE_CHECKING:
    from agent_kit.output import OutputSpec

try:
    import openai
    from openai.types.chat import ChatCompletionChunk
except ImportError as e:
    raise ImportError(
        "The 'openai' package is required for OpenAIProvider. "
        "Install it with: pip install agent-kit-ai[openai]"
    ) from e

_COST_TABLE: dict[str, tuple[float, float]] = {
    "gpt-4o":        (2.50, 10.00),
    "gpt-4o-mini":   (0.15, 0.60),
    "gpt-4-turbo":   (10.00, 30.00),
    "gpt-4":         (30.00, 60.00),
    "gpt-3.5-turbo": (0.50, 1.50),
    "o1":            (15.00, 60.00),
    "o1-mini":       (3.00, 12.00),
}

# Every model in _COST_TABLE that supports prompt caching bills cached prompt tokens at half the input rate
_CACHED_INPUT_MULTIPLIER = 0.5

_DEFAULT_MODEL = "gpt-4o"


def _estimate_cost(model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
    """USD for one call; ``input_tokens`` excludes the ``cached_tokens`` read from the prompt cache."""
    rates = lookup_rates(_COST_TABLE, model)
    if rates is None:
        return 0.0
    in_rate, out_rate = rates
    cached_rate = cached_input_rate(model)
    if cached_rate is None:
        cached_rate = in_rate * _CACHED_INPUT_MULTIPLIER
    return (input_tokens * in_rate + cached_tokens * cached_rate + output_tokens * out_rate) / 1_000_000


def _usage(usage: Any) -> tuple[int, int, int]:
    """(uncached input, cached input, output) tokens. OpenAI's prompt_tokens includes the cached ones."""
    if usage is None:
        return 0, 0, 0
    details = getattr(usage, "prompt_tokens_details", None)
    cached = int(getattr(details, "cached_tokens", 0) or 0)
    return int(usage.prompt_tokens or 0) - cached, cached, int(usage.completion_tokens or 0)


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


_REASONING_EFFORT = {"xhigh": "high", "max": "high"}


def _apply_options(call_kwargs: dict[str, Any], options: RequestOptions | None) -> None:
    """Passthrough and effort; thinking, caching, and context management don't apply to this API."""
    if options is None:
        return
    call_kwargs.update(options.provider_options)
    if options.effort:
        call_kwargs["reasoning_effort"] = _REASONING_EFFORT.get(options.effort, options.effort)


def _apply_response_format(call_kwargs: dict[str, Any], output_schema: OutputSpec[Any] | None) -> None:
    if output_schema is not None:
        call_kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": output_schema.name, "schema": output_schema.json_schema, "strict": True},
        }


def _messages_to_openai(messages: list[Message]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "tool":
            result.append(
                {
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id,
                    "content": msg.content,
                }
            )
        elif msg.role == "assistant" and msg.tool_calls:
            result.append(
                {
                    "role": "assistant",
                    "content": msg.content or None,
                    "tool_calls": [
                        {
                            "id": tc.call_id,
                            "type": "function",
                            "function": {"name": tc.tool_name, "arguments": json.dumps(tc.arguments)},
                        }
                        for tc in msg.tool_calls
                    ],
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

    supports_structured_output = True
    supports_request_options = True

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

    def _price(
        self, model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0
    ) -> tuple[float, bool]:
        """(cost_usd, priced) for one call."""
        priced = lookup_rates(_COST_TABLE, model) is not None
        return _estimate_cost(model, input_tokens, output_tokens, cached_tokens), priced

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        tools: list[ToolSchema] | None = None,
        system: str | None = None,
        max_tokens: int = 4096,
        output_schema: OutputSpec[Any] | None = None,
        options: RequestOptions | None = None,
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
        _apply_options(call_kwargs, options)
        _apply_response_format(call_kwargs, output_schema)

        t0 = time.monotonic()
        try:
            response = await self._client.chat.completions.create(**call_kwargs)
        except openai.APIError as exc:
            raise ProviderError(f"OpenAI API error: {exc}") from exc

        duration_ms = int((time.monotonic() - t0) * 1000)
        choice = response.choices[0]
        msg = choice.message
        if choice.finish_reason == "length":
            raise ResponseTruncatedError(self.name(), max_tokens, bool(msg.tool_calls))

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

        input_tokens, cached_tokens, output_tokens = _usage(response.usage)
        cost_usd, priced = self._price(resolved_model, input_tokens, output_tokens, cached_tokens)
        cost = CostSummary(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cached_tokens,
            total_tokens=input_tokens + cached_tokens + output_tokens,
            cost_usd=cost_usd,
            model=resolved_model,
            priced=priced,
        )

        text = msg.content or getattr(msg, "refusal", None) or ""
        assistant_msg = Message(role="assistant", content=text, tool_calls=tool_calls)
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
        tools: list[ToolSchema] | None = None,
        system: str | None = None,
        max_tokens: int = 4096,
        output_schema: OutputSpec[Any] | None = None,
        options: RequestOptions | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str | Turn]:
        resolved_model = model or self.config.default_model
        converted = _messages_to_openai(messages)

        if system and not any(m["role"] == "system" for m in converted):
            converted = [{"role": "system", "content": system}] + converted

        call_kwargs: dict[str, Any] = {
            "model": resolved_model,
            "messages": converted,
            "max_tokens": max_tokens,
            "stream_options": {"include_usage": True},
            **kwargs,
        }
        if tools:
            call_kwargs["tools"] = _to_openai_tools(tools)
            call_kwargs["tool_choice"] = "auto"
        _apply_options(call_kwargs, options)
        _apply_response_format(call_kwargs, output_schema)

        text_parts: list[str] = []
        pending: dict[int, dict[str, str]] = {}  # tool-call deltas by index
        input_tokens = cached_tokens = output_tokens = 0
        finish_reason: str | None = None
        t0 = time.monotonic()
        try:
            stream: openai.AsyncStream[ChatCompletionChunk] = (
                await self._client.chat.completions.create(stream=True, **call_kwargs)
            )
            async with stream:
                async for chunk in stream:
                    if chunk.usage:
                        input_tokens, cached_tokens, output_tokens = _usage(chunk.usage)
                    if not chunk.choices:
                        continue
                    finish_reason = getattr(chunk.choices[0], "finish_reason", None) or finish_reason
                    delta = chunk.choices[0].delta
                    if delta.content:
                        text_parts.append(delta.content)
                        yield delta.content
                    refusal = getattr(delta, "refusal", None)
                    if refusal:
                        text_parts.append(refusal)
                        yield refusal
                    for tc in delta.tool_calls or []:
                        slot = pending.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function and tc.function.name:
                            slot["name"] += tc.function.name
                        if tc.function and tc.function.arguments:
                            slot["arguments"] += tc.function.arguments
        except openai.APIError as exc:
            raise ProviderError(f"OpenAI stream error: {exc}") from exc

        if finish_reason is None:  # the connection closed before the final chunk
            raise ProviderError(f"{self.name()} stream ended before the response finished")
        if finish_reason == "length":
            raise ResponseTruncatedError(self.name(), max_tokens, bool(pending))
        tool_calls = [
            ToolCall(
                tool_name=slot["name"],
                arguments=json.loads(slot["arguments"] or "{}"),
                call_id=slot["id"],
            )
            for _, slot in sorted(pending.items())
        ]
        cost_usd, priced = self._price(resolved_model, input_tokens, output_tokens, cached_tokens)
        yield Turn(
            messages_in=messages,
            message_out=Message(role="assistant", content="".join(text_parts), tool_calls=tool_calls),
            tool_calls=tool_calls,
            cost=CostSummary(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cached_tokens,
                total_tokens=input_tokens + cached_tokens + output_tokens,
                cost_usd=cost_usd,
                model=resolved_model,
                priced=priced,
            ),
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
