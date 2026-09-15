"""Token budget planning and context management through the loop."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from agent_kit import Agent, AgentConfig, Compaction, tool
from agent_kit.memory.budget import message_chars, plan_trim, prompt_chars
from agent_kit.providers.base import ProviderConfig
from agent_kit.types import CostSummary, Message, RequestOptions, ToolCall, ToolSchema, Turn


def test_message_chars_counts_content_native_and_arguments():
    m = Message(
        role="assistant",
        content="abc",
        tool_calls=[ToolCall(tool_name="t", arguments={"k": "v"}, call_id="1")],
        native_content=[{"type": "text", "text": "abc"}],
        native_provider="anthropic",
    )
    assert message_chars(m) == 3 + len('{"k": "v"}') + len('[{"type": "text", "text": "abc"}]')


def test_prompt_chars_includes_system_and_tools():
    tool = ToolSchema(name="get", description="desc", parameters={"type": "object"})
    msgs = [Message(role="user", content="hello")]
    assert prompt_chars("sys", [tool], msgs) == 3 + len("get") + len("desc") + len('{"type": "object"}') + 5


def test_plan_trim_under_budget():
    msgs = [Message(role="user", content="x" * 100)]
    assert plan_trim(msgs, 100, budget_tokens=50, tokens_per_char=0.25) == (0, 25, 25)


def test_plan_trim_cuts_to_half_budget():
    msgs = [Message(role="user", content="x" * 400) for _ in range(10)]  # 4000 chars = 1000 tokens
    dropped, before, after = plan_trim(msgs, 4000, budget_tokens=600, tokens_per_char=0.25)
    assert (dropped, before, after) == (7, 1000, 300)


def test_plan_trim_keeps_last_message():
    msgs = [Message(role="user", content="x" * 4000), Message(role="user", content="y" * 4000)]
    assert plan_trim(msgs, 8000, budget_tokens=10, tokens_per_char=0.25) == (1, 2000, 1000)


class Scripted:
    config = ProviderConfig(default_model="scripted")

    def __init__(self, *turns: Turn, options: bool = True) -> None:
        self.turns = list(turns)
        self.requests: list[tuple[list[Message], dict[str, Any]]] = []
        if options:
            self.supports_request_options = True

    def name(self) -> str:
        return "scripted"

    async def complete(self, messages: list[Message], **kw: Any) -> Turn:
        self.requests.append((list(messages), kw))
        return self.turns.pop(0)

    async def stream(self, messages: list[Message], **kw: Any) -> Any:
        self.requests.append((list(messages), kw))
        yield self.turns.pop(0)


def final(text: str = "done", input_tokens: int = 0, events: list[dict[str, Any]] | None = None) -> Turn:
    return Turn(
        message_out=Message(role="assistant", content=text),
        cost=CostSummary(input_tokens=input_tokens),
        context_events=events or [],
    )


def call(input_tokens: int = 0) -> Turn:
    tc = [ToolCall(tool_name="lookup", arguments={}, call_id="c1")]
    return Turn(
        message_out=Message(role="assistant", content="", tool_calls=tc), tool_calls=tc,
        cost=CostSummary(input_tokens=input_tokens),
    )


@tool(description="look something up")
async def lookup() -> dict[str, Any]:
    return {"ok": True}


def audited(agent: Agent) -> list[tuple[str, dict[str, Any]]]:
    assert agent.audit is not None
    recorded: list[tuple[str, dict[str, Any]]] = []
    original = agent.audit.append

    def spy(event_type: str, actor: str, payload: dict[str, Any] | None = None) -> Any:
        recorded.append((event_type, payload or {}))
        return original(event_type, actor, payload)

    agent.audit.append = spy  # type: ignore[method-assign]
    return recorded


def seeded(agent: Agent, exchanges: int) -> None:
    for i in range(exchanges):
        agent.memory.add(Message(role="user", content=f"q{i}" + "x" * 398))
        agent.memory.add(Message(role="assistant", content=f"a{i}" + "y" * 398))


async def test_options_reach_providers_that_support_them():
    provider = Scripted(final())
    config = AgentConfig(effort="high", thinking="adaptive", provider_options={"seed": 1})
    await Agent(provider, config=config).run("hi")
    assert provider.requests[0][1]["options"] == RequestOptions(
        effort="high", thinking="adaptive", provider_options={"seed": 1}
    )


async def test_provider_without_options_gets_none_and_one_warning(caplog):
    provider = Scripted(final(), final(), options=False)
    agent = Agent(provider, config=AgentConfig(effort="high"))
    with caplog.at_level(logging.WARNING):
        await agent.run("hi")
    assert "options" not in provider.requests[0][1]
    assert sum("does not accept request options" in r.message for r in caplog.records) == 1

    quiet = Scripted(final(), options=False)
    caplog.clear()
    await Agent(quiet).run("hi")
    assert not any("does not accept request options" in r.message for r in caplog.records)


async def test_history_over_budget_is_cut_once_to_half():
    provider = Scripted(call(input_tokens=700), final(input_tokens=720))
    agent = Agent(provider, tools=[lookup], config=AgentConfig(context_budget_tokens=2_000))
    seeded(agent, 10)  # 20 messages × 400 chars ≈ 2_000 tokens at 0.25
    recorded = audited(agent)

    await agent.run("latest question")

    first, second = provider.requests[0][0], provider.requests[1][0]
    trims = [p for e, p in recorded if e == "context_trimmed"]
    assert len(trims) == 1
    assert trims[0]["turn"] == 1 and trims[0]["budget_tokens"] == 2_000
    assert trims[0]["estimated_tokens_before"] > 2_000 >= 2 * trims[0]["estimated_tokens_after"] - 100
    assert first[0].role == "user" and first[-1].content == "latest question"
    assert second[: len(first)] == first  # prefix unchanged between cuts


async def test_trim_never_splits_tool_pairs_and_keeps_latest_user():
    provider = Scripted(final())
    agent = Agent(provider, config=AgentConfig(context_budget_tokens=50))
    agent.memory.add(Message(role="user", content="u" * 400))
    for i in range(5):
        tc = [ToolCall(tool_name="lookup", arguments={}, call_id=f"c{i}")]
        agent.memory.add(Message(role="assistant", content="", tool_calls=tc))
        agent.memory.add(Message(role="tool", content="r" * 400, tool_call_id=f"c{i}"))

    await agent.run("now")

    sent = provider.requests[0][0]
    assert sent[-1].content == "now"
    for i, m in enumerate(sent):
        if m.role == "tool":
            assert sent[i - 1].tool_calls and sent[i - 1].tool_calls[0].call_id == m.tool_call_id


@pytest.mark.parametrize("config", [AgentConfig(context_budget_tokens=None), AgentConfig(context_budget_tokens=100, compaction=Compaction())])
async def test_no_client_trimming_when_disabled_or_compacting(config):
    provider = Scripted(final())
    agent = Agent(provider, config=config)
    seeded(agent, 10)
    await agent.run("q")
    assert len(provider.requests[0][0]) == 21


async def test_context_events_are_audited():
    events = [
        {"type": "compaction", "input_tokens": 180_000, "output_tokens": 3_500},
        {"type": "clear_tool_uses_20250919", "cleared_tool_uses": 4, "cleared_input_tokens": 51_000},
    ]
    agent = Agent(Scripted(final(events=events)))
    recorded = audited(agent)

    await agent.run("hi")

    assert [p for e, p in recorded if e == "context_compacted"] == [
        {"turn": 1, "input_tokens": 180_000, "output_tokens": 3_500}
    ]
    assert [p for e, p in recorded if e == "context_edited"] == [
        {"turn": 1, "edit": "clear_tool_uses_20250919", "cleared_tool_uses": 4, "cleared_input_tokens": 51_000}
    ]


def test_agent_config_defaults():
    config = AgentConfig()
    assert (config.context_budget_tokens, config.memory_window, config.prompt_caching) == (150_000, None, True)
