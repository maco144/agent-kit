"""Anthropic Claude provider adapter."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, AsyncIterator

from agent_kit.exceptions import ProviderError, ResponseTruncatedError
from agent_kit.providers.base import ProviderConfig
from agent_kit.providers.pricing import lookup_rates
from agent_kit.types import CostSummary, Message, RequestOptions, ToolCall, ToolSchema, Turn

if TYPE_CHECKING:
    from agent_kit.output import OutputSpec

try:
    import anthropic
except ImportError as e:
    raise ImportError(
        "The 'anthropic' package is required. Install it with: pip install anthropic"
    ) from e


# USD per million tokens (input, output). Longest matching prefix wins.
# Advisory — actual billing comes from the Anthropic console.
_COST_TABLE: dict[str, tuple[float, float]] = {
    "claude-fable-5":    (10.00, 50.00),
    "claude-mythos-5":   (10.00, 50.00),
    "claude-opus-5":     (5.00,  25.00),
    "claude-opus-4-8":   (5.00,  25.00),
    "claude-opus-4-7":   (5.00,  25.00),
    "claude-opus-4-6":   (5.00,  25.00),
    "claude-opus-4-5":   (5.00,  25.00),
    "claude-opus-4":     (15.00, 75.00),  # Opus 4 / 4.1
    "claude-sonnet-5":   (2.00,  10.00),
    "claude-sonnet-4":   (3.00,  15.00),  # Sonnet 4 / 4.5 / 4.6
    "claude-haiku-4-5":  (1.00,  5.00),
    "claude-3-7-sonnet": (3.00,  15.00),
    "claude-3-5-sonnet": (3.00,  15.00),
    "claude-3-5-haiku":  (0.80,  4.00),
    "claude-3-opus":     (15.00, 75.00),
    "claude-3-haiku":    (0.25,  1.25),
}

# Cache reads bill at 0.1x input except where listed; 5-minute cache writes at 1.25x.
_CACHE_READ_MULTIPLIER: dict[str, float] = {"claude-fable-5-1": 0.025}
_CACHE_WRITE_MULTIPLIER = 1.25

_DEFAULT_MODEL = "claude-opus-5"

# Stop reasons that mean the output was cut off: a tool_use input or the answer is incomplete
_TRUNCATED_STOP_REASONS = frozenset({"max_tokens", "model_context_window_exceeded"})

_COMPACTION_BETA = "compact-2026-01-12"
_CONTEXT_EDITING_BETA = "context-management-2025-06-27"


def _estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    rates = lookup_rates(_COST_TABLE, model)
    if rates is None:
        return 0.0
    in_rate, out_rate = rates
    read_multiplier = next(
        (m for prefix, m in _CACHE_READ_MULTIPLIER.items() if model.startswith(prefix)), 0.1
    )
    return (
        input_tokens * in_rate
        + output_tokens * out_rate
        + cache_read_tokens * in_rate * read_multiplier
        + cache_write_tokens * in_rate * _CACHE_WRITE_MULTIPLIER
    ) / 1_000_000


def _check_complete(response: Any, max_tokens: int) -> None:
    """Refuse a cut-off response rather than run a tool on partial input or return half an answer."""
    if getattr(response, "stop_reason", None) in _TRUNCATED_STOP_REASONS:
        had_tool_calls = any(getattr(b, "type", None) == "tool_use" for b in response.content)
        raise ResponseTruncatedError("anthropic", max_tokens, had_tool_calls)


def _get(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def _plain(value: Any) -> Any:
    """SDK content blocks (or test doubles) as JSON-ready dicts, dropping unset fields."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if hasattr(value, "__dict__"):
        return {k: _plain(v) for k, v in vars(value).items() if v is not None}
    return value


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

    Assistant tool calls become ``tool_use`` blocks. Consecutive tool results are
    merged into one user message, as the API expects for parallel tool use.

    Returns (system_text | None, anthropic_messages).
    """
    system_text: str | None = None
    result: list[dict[str, Any]] = []

    for msg in messages:
        if msg.role == "system":
            system_text = msg.content
        elif msg.role == "tool":
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": msg.tool_call_id,
                "content": msg.content,
            }
            if msg.metadata.get("is_error"):
                block["is_error"] = True
            prev = result[-1] if result else None
            if (
                prev is not None
                and prev["role"] == "user"
                and isinstance(prev["content"], list)
                and prev["content"][-1].get("type") == "tool_result"
            ):
                prev["content"].append(block)
            else:
                result.append({"role": "user", "content": [block]})
        elif msg.role == "assistant" and msg.native_provider == "anthropic" and msg.native_content:
            # Verbatim: thinking signatures and compaction blocks must come back unchanged
            result.append({"role": "assistant", "content": list(msg.native_content)})
        elif msg.role == "assistant" and msg.tool_calls:
            blocks: list[dict[str, Any]] = []
            if msg.content:
                blocks.append({"type": "text", "text": msg.content})
            blocks.extend(
                {"type": "tool_use", "id": tc.call_id, "name": tc.tool_name, "input": tc.arguments}
                for tc in msg.tool_calls
            )
            result.append({"role": "assistant", "content": blocks})
        elif msg.role == "assistant" and not msg.content:
            continue  # the API rejects empty assistant text
        else:
            result.append({"role": msg.role, "content": msg.content})

    return system_text, result


def _apply_output_schema(call_kwargs: dict[str, Any], output_schema: OutputSpec[Any] | None) -> None:
    """Constrain the answer with output_config.format, keeping other output_config keys (effort)."""
    if output_schema is not None:
        call_kwargs["output_config"] = {
            **call_kwargs.get("output_config", {}),
            "format": {"type": "json_schema", "schema": output_schema.json_schema},
        }


def _merge(call_kwargs: dict[str, Any], key: str, values: dict[str, Any]) -> None:
    call_kwargs[key] = {**call_kwargs.get(key, {}), **values}


def _apply_options(call_kwargs: dict[str, Any], options: RequestOptions | None) -> list[str]:
    """Apply thinking, effort, caching, context management, and passthrough; return the betas needed."""
    if options is None:
        return []
    extra = dict(options.provider_options)
    betas = set(extra.pop("betas", None) or [])
    for key in ("output_config", "thinking"):
        if key in extra:
            _merge(call_kwargs, key, extra.pop(key))
    call_kwargs.update(extra)
    if options.thinking:
        _merge(call_kwargs, "thinking", {"type": options.thinking})
    if options.effort:
        _merge(call_kwargs, "output_config", {"effort": options.effort})
    if options.prompt_caching:
        system = call_kwargs.get("system")
        if isinstance(system, str) and system:
            call_kwargs["system"] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]
        call_kwargs["cache_control"] = {"type": "ephemeral"}  # automatic breakpoint on the conversation
    edits: list[dict[str, Any]] = []
    if options.clear_tool_results:
        ctr = options.clear_tool_results
        edit: dict[str, Any] = {
            "type": "clear_tool_uses_20250919",
            "trigger": {"type": "input_tokens", "value": ctr.trigger_tokens},
            "keep": {"type": "tool_uses", "value": ctr.keep},
        }
        if ctr.exclude_tools:
            edit["exclude_tools"] = list(ctr.exclude_tools)
        if ctr.clear_inputs:
            edit["clear_tool_inputs"] = True
        edits.append(edit)
        betas.add(_CONTEXT_EDITING_BETA)
    if options.compaction:
        compact: dict[str, Any] = {
            "type": "compact_20260112",
            "trigger": {"type": "input_tokens", "value": options.compaction.trigger_tokens},
        }
        if options.compaction.instructions:
            compact["instructions"] = options.compaction.instructions
        edits.append(compact)
        betas.add(_COMPACTION_BETA)
    if edits:
        call_kwargs["context_management"] = {"edits": edits}
    return sorted(betas)


def _turn_from_response(
    response: Any, messages: list[Message], model: str, duration_ms: int
) -> Turn:
    """Build a Turn (text, tool calls, cache-aware cost) from a Messages API response."""
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

    # With compaction, top-level usage excludes the summarisation call; billing is the sum of iterations
    iterations = _get(response.usage, "iterations") or []
    parts = iterations or [response.usage]

    def total(field: str) -> int:
        return sum(int(_get(p, field) or 0) for p in parts)

    input_tokens, output_tokens = total("input_tokens"), total("output_tokens")
    cache_read, cache_write = total("cache_read_input_tokens"), total("cache_creation_input_tokens")
    cost = CostSummary(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        total_tokens=input_tokens + output_tokens + cache_read + cache_write,
        cost_usd=_estimate_cost(model, input_tokens, output_tokens, cache_read, cache_write),
        model=model,
    )
    context_events: list[dict[str, Any]] = [
        {
            "type": "compaction",
            "input_tokens": int(_get(it, "input_tokens") or 0),
            "output_tokens": int(_get(it, "output_tokens") or 0),
        }
        for it in iterations
        if _get(it, "type") == "compaction"
    ]
    context_management = getattr(response, "context_management", None)
    context_events.extend(_plain(edit) for edit in (_get(context_management, "applied_edits") or []))
    return Turn(
        messages_in=messages,
        message_out=Message(
            role="assistant",
            content=" ".join(text_parts),
            tool_calls=tool_calls,
            native_content=[_plain(block) for block in response.content],
            native_provider="anthropic",
        ),
        tool_calls=tool_calls,
        cost=cost,
        duration_ms=duration_ms,
        context_events=context_events,
    )


class AnthropicProvider:
    """
    LLM provider adapter for Anthropic Claude.

    Usage::

        provider = AnthropicProvider()                        # uses ANTHROPIC_API_KEY env var
        provider = AnthropicProvider(api_key="sk-ant-...")   # explicit key
        provider = AnthropicProvider(default_model="claude-sonnet-5")
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

        self._client = anthropic.AsyncAnthropic(**kwargs)

    def name(self) -> str:
        return "anthropic"

    def _request(
        self,
        messages: list[Message],
        model: str | None,
        tools: list[ToolSchema] | None,
        system: str | None,
        max_tokens: int,
        output_schema: OutputSpec[Any] | None,
        options: RequestOptions | None,
        kwargs: dict[str, Any],
    ) -> tuple[str, dict[str, Any], list[str]]:
        """Build call kwargs shared by complete() and stream(); returns (model, kwargs, betas)."""
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
        betas = _apply_options(call_kwargs, options)
        _apply_output_schema(call_kwargs, output_schema)
        return resolved_model, call_kwargs, betas

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
        resolved_model, call_kwargs, betas = self._request(
            messages, model, tools, system, max_tokens, output_schema, options, kwargs
        )
        t0 = time.monotonic()
        try:
            if betas:
                response = await self._client.beta.messages.create(betas=betas, **call_kwargs)
            else:
                response = await self._client.messages.create(**call_kwargs)
        except anthropic.APIError as exc:
            raise ProviderError(f"Anthropic API error: {exc}") from exc

        _check_complete(response, max_tokens)
        return _turn_from_response(
            response, messages, resolved_model, int((time.monotonic() - t0) * 1000)
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
        resolved_model, call_kwargs, betas = self._request(
            messages, model, tools, system, max_tokens, output_schema, options, kwargs
        )
        manager: Any = (
            self._client.beta.messages.stream(betas=betas, **call_kwargs)
            if betas
            else self._client.messages.stream(**call_kwargs)
        )
        t0 = time.monotonic()
        try:
            async with manager as stream:
                async for text in stream.text_stream:
                    yield text
                final = await stream.get_final_message()
        except anthropic.APIError as exc:
            raise ProviderError(f"Anthropic stream error: {exc}") from exc

        if getattr(final, "stop_reason", None) is None:  # the connection closed before message_stop
            raise ProviderError("Anthropic stream ended before the message finished")
        _check_complete(final, max_tokens)
        yield _turn_from_response(
            final, messages, resolved_model, int((time.monotonic() - t0) * 1000)
        )
