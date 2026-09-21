"""Runs that end mid tool call: cancellation, crashes over persistent memory, and what the next run sends."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_kit import Agent, AgentConfig, tool
from agent_kit.cloud.models import CloudEvent
from agent_kit.cloud.reporter import CloudReporter
from agent_kit.durable import SQLiteRunStore
from agent_kit.memory import SQLiteMemory
from agent_kit.providers.base import ProviderConfig
from agent_kit.types import CostSummary, Message, RetryPolicyConfig, ToolCall, Turn


class SimulatedCrash(BaseException):
    """Stands in for a killed process: escapes every `except Exception`."""


class Scripted:
    config = ProviderConfig(default_model="scripted")

    def __init__(self, *turns: Turn) -> None:
        self.turns = list(turns)
        self.requests: list[list[Message]] = []

    def name(self) -> str:
        return "scripted"

    async def complete(self, messages: list[Message], **kw: Any) -> Turn:
        self.requests.append(list(messages))
        return self.turns.pop(0)


def calls(*names: str) -> Turn:
    tcs = [ToolCall(tool_name=n, arguments={}, call_id=f"{n}-{i}") for i, n in enumerate(names)]
    return Turn(message_out=Message(role="assistant", content="", tool_calls=tcs), tool_calls=tcs,
                cost=CostSummary(total_tokens=10, cost_usd=0.01))


def final(text: str = "done") -> Turn:
    return Turn(message_out=Message(role="assistant", content=text), cost=CostSummary(total_tokens=5, cost_usd=0.01))


@tool(description="hangs until cancelled")
async def hang() -> dict[str, Any]:
    await asyncio.sleep(3600)
    return {}


@tool(description="returns at once")
async def quick() -> dict[str, Any]:
    return {"ok": True}


@tool(description="dies with the process")
async def crash() -> dict[str, Any]:
    raise SimulatedCrash


def assert_every_tool_call_answered(messages: list[Message]) -> None:
    """What the Anthropic and OpenAI APIs require: each tool call is followed by its result, before anything else."""
    for i, m in enumerate(messages):
        if m.role != "assistant" or not m.tool_calls:
            continue
        following: list[str] = []
        for later in messages[i + 1:]:
            if later.role != "tool":
                break
            following.append(later.tool_call_id or "")
        assert sorted(following) == sorted(tc.call_id for tc in m.tool_calls), messages


async def test_run_after_a_cancelled_tool_call_answers_every_call():
    provider = Scripted(calls("quick", "hang"), final("second"))
    agent = Agent(provider, tools=[quick, hang])
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(agent.run("first"), timeout=0.2)

    result = await agent.run("second prompt")

    assert result.output == "second"
    assert_every_tool_call_answered(provider.requests[-1])
    answers = {m.tool_call_id: m for m in provider.requests[-1] if m.role == "tool"}
    assert answers["quick-0"].content == '{"ok": true}'
    assert answers["hang-1"].metadata == {"is_error": True}


async def test_run_after_a_crash_over_persistent_memory_answers_every_call(tmp_path):
    path = tmp_path / "memory.db"
    with pytest.raises(SimulatedCrash):
        await Agent(Scripted(calls("crash")), tools=[crash], memory=SQLiteMemory(path)).run("first")

    provider = Scripted(final("recovered"))
    result = await Agent(provider, tools=[crash], memory=SQLiteMemory(path)).run("second prompt")

    assert result.output == "recovered"
    assert_every_tool_call_answered(provider.requests[-1])


async def test_cancelled_run_is_marked_failed_and_reported(tmp_path):
    reporter = CloudReporter(api_key="akt_test", project="proj", agent_name="a")
    events: list[CloudEvent] = []

    async def enqueue(event: CloudEvent) -> None:
        events.append(event)

    reporter._enqueue = enqueue  # type: ignore[method-assign]
    agent = Agent(Scripted(calls("hang")), tools=[hang], config=AgentConfig(
        run_store=SQLiteRunStore(tmp_path / "runs.db"), cloud=reporter,
        retry_policy=RetryPolicyConfig(max_attempts=1)))

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(agent.run("go", run_id="r1"), timeout=0.2)

    stored = await SQLiteRunStore(tmp_path / "runs.db").load("r1")
    assert stored is not None and stored.status == "failed"
    assert stored.error is not None and stored.error.startswith("CancelledError")
    assert "run_error" in [e.event_type.value for e in events]
