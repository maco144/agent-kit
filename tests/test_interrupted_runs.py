"""Runs that end mid tool call or stall mid model call: cancellation, crashes, deadlines."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_kit import Agent, AgentConfig, tool
from agent_kit.cloud.models import CloudEvent
from agent_kit.cloud.reporter import CloudReporter
from agent_kit.durable import SQLiteRunStore
from agent_kit.exceptions import ProviderError
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


hanging: list[asyncio.Event] = []  # one per run, created on the test's own event loop


@tool(description="hangs until cancelled")
async def hang() -> dict[str, Any]:
    hanging[-1].set()
    await asyncio.sleep(3600)
    return {}


async def cancel_once_hanging(run: Any) -> None:
    """Start a run, cancel it once the hang tool is executing (not on a wall clock), and await the cancellation."""
    hanging.append(asyncio.Event())
    task = asyncio.ensure_future(run)
    await asyncio.wait_for(hanging[-1].wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


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
    await cancel_once_hanging(agent.run("first"))

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

    await cancel_once_hanging(agent.run("go", run_id="r1"))

    stored = await SQLiteRunStore(tmp_path / "runs.db").load("r1")
    assert stored is not None and stored.status == "failed"
    assert stored.error is not None and stored.error.startswith("CancelledError")
    assert "run_error" in [e.event_type.value for e in events]


# ---------------------------------------------------------------------------
# Model calls that stall
# ---------------------------------------------------------------------------


class Stalling:
    """Answers after ``stalls`` hung calls; a stream yields one chunk, then hangs (a stall behind keepalive pings)."""

    config = ProviderConfig(default_model="stalling")

    def __init__(self, stalls: int = 0) -> None:
        self.stalls = stalls
        self.calls = 0

    def name(self) -> str:
        return "stalling"

    async def complete(self, messages: list[Message], **kw: Any) -> Turn:
        self.calls += 1
        if self.calls <= self.stalls:
            await asyncio.sleep(3600)
        return final("answered")

    async def stream(self, messages: list[Message], **kw: Any) -> Any:
        self.calls += 1
        yield "partial "
        await asyncio.sleep(3600)
        yield final("never")


async def test_a_stalled_model_call_is_retried():
    provider = Stalling(stalls=1)
    agent = Agent(provider, config=AgentConfig(llm_timeout_s=1.0))
    result = await asyncio.wait_for(agent.run("go"), timeout=5)
    assert (result.output, provider.calls) == ("answered", 2)


async def test_a_stream_that_stalls_after_text_fails_instead_of_hanging():
    agent = Agent(Stalling(), config=AgentConfig(llm_timeout_s=1.0))
    chunks: list[str] = []
    with pytest.raises(ProviderError, match="within 1s"):
        async for chunk in agent.stream("go"):
            chunks.append(chunk)
    assert chunks == ["partial "]


def test_model_calls_have_a_default_deadline():
    assert AgentConfig().llm_timeout_s == 600.0
