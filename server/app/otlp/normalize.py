"""
Map GenAI spans onto one shape.

Supports OpenTelemetry GenAI semantic conventions (``gen_ai.*``) and OpenInference
(``openinference.span.kind``). Only names, counts, identifiers and status are
copied — prompt, completion, and tool content attributes are never read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.otlp.decode import RawSpan

_SEMCONV_KINDS = {
    "chat": "llm",
    "generate_content": "llm",
    "text_completion": "llm",
    "execute_tool": "tool",
    "invoke_agent": "agent",
    "invoke_workflow": "agent",
}
_OPENINFERENCE_KINDS = {"LLM": "llm", "TOOL": "tool", "AGENT": "agent", "CHAIN": "agent"}


@dataclass
class GenAISpan:
    raw: RawSpan
    kind: str  # "llm" | "tool" | "agent"
    convention: str  # "otel-genai" | "openinference"
    name: str
    model: str
    input_tokens: int  # uncached input
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    tool_call_id: str
    conversation_id: str
    failed: bool

    @property
    def duration_ms(self) -> int:
        return max(0, (self.raw.end_ns - self.raw.start_ns) // 1_000_000)


def normalize(span: RawSpan) -> GenAISpan | None:
    """Return the GenAI view of a span, or None if neither convention applies."""
    operation = span.attributes.get("gen_ai.operation.name")
    if isinstance(operation, str) and operation in _SEMCONV_KINDS:
        return _semconv(span, _SEMCONV_KINDS[operation])
    kind = span.attributes.get("openinference.span.kind")
    if isinstance(kind, str) and kind.upper() in _OPENINFERENCE_KINDS:
        return _openinference(span, _OPENINFERENCE_KINDS[kind.upper()])
    return None


def _semconv(span: RawSpan, kind: str) -> GenAISpan:
    a = span.attributes
    if kind == "agent":
        name = _str(a.get("gen_ai.agent.name")) or _str(a.get("gen_ai.workflow.name")) or _target(span.name)
    elif kind == "tool":
        name = _str(a.get("gen_ai.tool.name")) or _target(span.name)
    else:
        name = ""
    cache_read = _int(a.get("gen_ai.usage.cache_read.input_tokens"))
    cache_write = _int(a.get("gen_ai.usage.cache_write.input_tokens"))
    return GenAISpan(
        raw=span,
        kind=kind,
        convention="otel-genai",
        name=name,
        model=_str(a.get("gen_ai.response.model")) or _str(a.get("gen_ai.request.model")),
        input_tokens=max(0, _int(a.get("gen_ai.usage.input_tokens")) - cache_read - cache_write),
        output_tokens=_int(a.get("gen_ai.usage.output_tokens")),
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        tool_call_id=_str(a.get("gen_ai.tool.call.id")),
        conversation_id=_str(a.get("gen_ai.conversation.id")),
        failed=span.status_error or bool(a.get("error.type")),
    )


def _openinference(span: RawSpan, kind: str) -> GenAISpan:
    a = span.attributes
    if kind == "agent":
        name = _str(a.get("agent.name")) or span.name
    elif kind == "tool":
        name = _str(a.get("tool.name")) or span.name
    else:
        name = ""
    cache_read = _int(a.get("llm.token_count.prompt_details.cache_read"))
    cache_write = _int(a.get("llm.token_count.prompt_details.cache_write"))
    return GenAISpan(
        raw=span,
        kind=kind,
        convention="openinference",
        name=name,
        model=_str(a.get("llm.model_name")),
        input_tokens=max(0, _int(a.get("llm.token_count.prompt")) - cache_read - cache_write),
        output_tokens=_int(a.get("llm.token_count.completion")),
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        tool_call_id=_str(a.get("tool_call.id")) or _str(a.get("tool.id")),
        conversation_id=_str(a.get("session.id")),
        failed=span.status_error,
    )


def _target(span_name: str) -> str:
    """'invoke_agent billing' → 'billing'; names without an operation prefix pass through."""
    _, _, rest = span_name.partition(" ")
    return rest or span_name


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
