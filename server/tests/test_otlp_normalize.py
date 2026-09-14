from __future__ import annotations

import dataclasses

import pytest

from app.otlp.decode import RawSpan
from app.otlp.normalize import normalize
from app.otlp.pricing import estimate_cost

CONTENT = {
    "gen_ai.input.messages": '[{"role":"user","parts":[{"type":"text","content":"secret"}]}]',
    "gen_ai.output.messages": "secret answer",
    "gen_ai.system_instructions": "secret system",
    "gen_ai.tool.call.arguments": '{"card": "4111"}',
    "gen_ai.tool.call.result": "secret result",
    "llm.input_messages": "secret",
    "input.value": "secret",
    "output.value": "secret",
}


def raw(attributes: dict, name: str = "span", error: bool = False, duration_ms: int = 250) -> RawSpan:
    return RawSpan(
        trace_id="a" * 32, span_id="b" * 16, parent_span_id="", name=name,
        start_ns=1_000_000_000, end_ns=1_000_000_000 + duration_ms * 1_000_000,
        status_error=error, status_message="", attributes=attributes, resource={},
    )


def test_semconv_chat_span():
    span = normalize(raw({
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "anthropic",
        "gen_ai.request.model": "claude-opus-5",
        "gen_ai.response.model": "claude-opus-5-20260801",
        "gen_ai.usage.input_tokens": 1500,
        "gen_ai.usage.output_tokens": 200,
        "gen_ai.usage.cache_read.input_tokens": 1000,
        "gen_ai.usage.cache_write.input_tokens": 100,
        "gen_ai.conversation.id": "conv-1",
        **CONTENT,
    }, name="chat claude-opus-5"))

    assert span is not None
    assert (span.kind, span.convention) == ("llm", "otel-genai")
    assert span.model == "claude-opus-5-20260801"
    assert (span.input_tokens, span.output_tokens) == (400, 200)
    assert (span.cache_read_tokens, span.cache_write_tokens) == (1000, 100)
    assert (span.conversation_id, span.failed, span.duration_ms) == ("conv-1", False, 250)


@pytest.mark.parametrize(("operation", "kind"), [
    ("generate_content", "llm"), ("text_completion", "llm"),
    ("execute_tool", "tool"), ("invoke_agent", "agent"), ("invoke_workflow", "agent"),
])
def test_semconv_operation_kinds(operation, kind):
    span = normalize(raw({"gen_ai.operation.name": operation}))
    assert span is not None and span.kind == kind


def test_semconv_tool_and_agent_names():
    tool = normalize(raw({"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "lookup_order",
                          "gen_ai.tool.call.id": "call_9", "error.type": "timeout"}))
    agent = normalize(raw({"gen_ai.operation.name": "invoke_agent"}, name="invoke_agent billing"))
    workflow = normalize(raw({"gen_ai.operation.name": "invoke_workflow", "gen_ai.workflow.name": "refunds"}))

    assert tool is not None and (tool.name, tool.tool_call_id, tool.failed) == ("lookup_order", "call_9", True)
    assert agent is not None and agent.name == "billing"
    assert workflow is not None and workflow.name == "refunds"


def test_openinference_llm_span():
    span = normalize(raw({
        "openinference.span.kind": "LLM",
        "llm.model_name": "gpt-4o",
        "llm.token_count.prompt": 900,
        "llm.token_count.completion": 50,
        "llm.token_count.prompt_details.cache_read": 400,
        "session.id": "sess-7",
        **CONTENT,
    }, error=True))

    assert span is not None
    assert (span.kind, span.convention, span.model) == ("llm", "openinference", "gpt-4o")
    assert (span.input_tokens, span.output_tokens, span.cache_read_tokens) == (500, 50, 400)
    assert (span.conversation_id, span.failed) == ("sess-7", True)


@pytest.mark.parametrize(("kind", "expected"), [("TOOL", "tool"), ("AGENT", "agent"), ("CHAIN", "agent"), ("llm", "llm")])
def test_openinference_kinds(kind, expected):
    span = normalize(raw({"openinference.span.kind": kind, "tool.name": "search", "tool.id": "t1", "agent.name": "planner"}))
    assert span is not None and span.kind == expected


def test_openinference_names():
    tool = normalize(raw({"openinference.span.kind": "TOOL", "tool.name": "search", "tool_call.id": "tc_1"}))
    agent = normalize(raw({"openinference.span.kind": "AGENT"}, name="AgentExecutor"))
    assert tool is not None and (tool.name, tool.tool_call_id) == ("search", "tc_1")
    assert agent is not None and agent.name == "AgentExecutor"


@pytest.mark.parametrize("attributes", [
    {},
    {"http.method": "POST"},
    {"gen_ai.operation.name": "embeddings"},
    {"gen_ai.operation.name": "create_agent"},
    {"openinference.span.kind": "RETRIEVER"},
    {"openinference.span.kind": "EMBEDDING"},
])
def test_non_genai_spans_are_ignored(attributes):
    assert normalize(raw(attributes)) is None


def test_no_content_attributes_survive_normalization():
    span = normalize(raw({"gen_ai.operation.name": "chat", **CONTENT}))
    assert span is not None
    values = {k: v for k, v in dataclasses.asdict(span).items() if k != "raw"}
    assert not any("secret" in str(v) or "4111" in str(v) for v in values.values())


def test_garbage_token_counts_become_zero():
    span = normalize(raw({"gen_ai.operation.name": "chat", "gen_ai.usage.input_tokens": "lots",
                          "gen_ai.usage.output_tokens": None}))
    assert span is not None and (span.input_tokens, span.output_tokens) == (0, 0)


@pytest.mark.parametrize(("model", "args", "usd"), [
    ("claude-opus-5", (1_000_000, 1_000_000), 30.0),
    ("claude-opus-4-8-20260101", (1_000_000, 0), 5.0),
    ("claude-sonnet-5", (0, 1_000_000), 10.0),
    ("claude-haiku-4-5", (1_000_000, 0), 1.0),
    ("gpt-4o-mini", (1_000_000, 0), 0.15),
    ("gpt-4o", (1_000_000, 0), 2.5),
    ("mystery-model", (1_000_000, 1_000_000), 0.0),
    ("", (1_000_000, 1_000_000), 0.0),
])
def test_pricing(model, args, usd):
    assert estimate_cost(model, *args) == pytest.approx(usd)


def test_pricing_cache_multipliers():
    assert estimate_cost("claude-opus-5", 0, 0, 1_000_000, 1_000_000) == pytest.approx(0.5 + 6.25)
    assert estimate_cost("claude-fable-5-1", 0, 0, 1_000_000) == pytest.approx(0.25)
