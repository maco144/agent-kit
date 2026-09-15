"""Durable runs: checkpoints, suspension for approval, crash recovery, concurrent resume."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from agent_kit import SUSPEND, Agent, AgentConfig, tool
from agent_kit.durable import SQLiteRunStore
from agent_kit.exceptions import CheckpointError, RunConflictError, RunNotFoundError
from agent_kit.hooks import Hooks, require_approval
from agent_kit.providers.base import ProviderConfig
from agent_kit.types import CostSummary, Message, RetryPolicyConfig, ToolCall, Turn


class SimulatedCrash(BaseException):
    """Stands in for a killed process: escapes every `except Exception`."""


class Scripted:
    config = ProviderConfig(default_model="scripted")

    def __init__(self, *turns: Turn | BaseException) -> None:
        self.turns = list(turns)
        self.requests: list[list[Message]] = []

    def name(self) -> str:
        return "scripted"

    async def complete(self, messages: list[Message], **kw: Any) -> Turn:
        self.requests.append(list(messages))
        item = self.turns.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def stream(self, messages: list[Message], **kw: Any) -> Any:
        turn = await self.complete(messages, **kw)
        if turn.message_out and turn.message_out.content:
            yield turn.message_out.content
        yield turn


def calls(*specs: tuple[str, dict[str, Any]]) -> Turn:
    tcs = [ToolCall(tool_name=n, arguments=a, call_id=f"{n}-{i}") for i, (n, a) in enumerate(specs)]
    return Turn(message_out=Message(role="assistant", content="", tool_calls=tcs), tool_calls=tcs,
                cost=CostSummary(total_tokens=10, cost_usd=0.01))


def final(text: str = "done") -> Turn:
    return Turn(message_out=Message(role="assistant", content=text), cost=CostSummary(total_tokens=5, cost_usd=0.01))


executed: list[str] = []
crash_in: set[str] = set()


@tool(description="refund an order")
async def refund(order_id: str) -> dict[str, Any]:
    executed.append(f"refund:{order_id}")
    if "refund" in crash_in:
        raise SimulatedCrash
    return {"refunded": order_id}


@tool(description="look up an order", idempotent=True)
async def lookup(order_id: str) -> dict[str, Any]:
    executed.append(f"lookup:{order_id}")
    if "lookup" in crash_in:
        raise SimulatedCrash
    return {"order_id": order_id, "status": "paid"}


@pytest.fixture(autouse=True)
def _reset():
    executed.clear()
    crash_in.clear()


@pytest.fixture
def db(tmp_path):
    return tmp_path / "runs.db"


def agent(db, provider: Scripted, approvals: bool = True, **config: Any) -> Agent:
    """A fresh Agent over a fresh store connection — stands in for another process."""
    hooks = Hooks(before_tool=[require_approval("refund")]) if approvals else None
    return Agent(provider, tools=[refund, lookup], config=AgentConfig(
        run_store=SQLiteRunStore(db), hooks=hooks, approver=SUSPEND if approvals else None,
        retry_policy=RetryPolicyConfig(max_attempts=1), **config,
    ))


def tool_messages(provider: Scripted) -> list[Message]:
    return [m for m in provider.requests[-1] if m.role == "tool"]


def event_types(a: Agent) -> list[str]:
    assert a.audit is not None
    return [e.event_type for e in a.audit.events()]


async def test_suspend_then_resume_approved_in_another_process(db):
    first = agent(db, Scripted(calls(("refund", {"order_id": "42"}))))
    result = await first.run("refund order 42", run_id="t1")

    assert (result.status, result.run_id, result.output) == ("suspended", "t1", "")
    assert [(p.call_id, p.tool_name, p.arguments) for p in result.pending_approvals] == [
        ("refund-0", "refund", {"order_id": "42"})
    ]
    assert executed == []
    assert (await SQLiteRunStore(db).load("t1")).status == "suspended"

    provider = Scripted(final("Refunded."))
    second = agent(db, provider)
    done = await second.resume("t1", approvals={"refund-0": True})

    assert (done.status, done.output, done.run_id) == ("completed", "Refunded.", "t1")
    assert executed == ["refund:42"]
    assert tool_messages(provider)[0].content == '{"refunded": "42"}'
    assert second.audit is not None and second.audit.verify()
    types = event_types(second)
    assert types.index("run_suspended") < types.index("run_resumed") < types.index("approval_granted")
    assert done.total_cost_usd == pytest.approx(0.02)
    assert (await SQLiteRunStore(db).load("t1")).status == "completed"


async def test_resume_denied(db):
    await agent(db, Scripted(calls(("refund", {"order_id": "42"})))).run("refund", run_id="t1")
    provider = Scripted(final("Could not refund."))
    done = await agent(db, provider).resume("t1", approvals={"refund-0": False})
    assert executed == []
    assert tool_messages(provider)[0].content == "Error: Tool call denied: approval denied"
    assert done.status == "completed"


async def test_mixed_turn_results_reach_memory_together_in_order(db):
    first = agent(db, Scripted(calls(("lookup", {"order_id": "1"}), ("refund", {"order_id": "1"}))))
    result = await first.run("check then refund", run_id="t1")
    assert result.status == "suspended" and executed == ["lookup:1"]
    assert first.memory.history()[-1].role == "assistant"

    provider = Scripted(final())
    await agent(db, provider).resume("t1", approvals={"refund-0": True, "refund-1": True, "unknown": True})
    assert [m.tool_call_id for m in tool_messages(provider)] == ["lookup-0", "refund-1"]
    assert executed == ["lookup:1", "refund:1"]


async def test_partial_answers_stay_suspended_without_model_call(db):
    await agent(db, Scripted(calls(("refund", {"order_id": "1"}), ("refund", {"order_id": "2"})))).run("two", run_id="t1")

    idle = Scripted()  # any model call would fail on pop
    partial = await agent(db, idle).resume("t1", approvals={"refund-0": True})
    assert partial.status == "suspended"
    assert [p.call_id for p in partial.pending_approvals] == ["refund-1"]
    assert idle.requests == [] and executed == ["refund:1"]

    provider = Scripted(final())
    done = await agent(db, provider).resume("t1", approvals={"refund-1": True})
    assert done.status == "completed" and executed == ["refund:1", "refund:2"]
    assert [m.tool_call_id for m in tool_messages(provider)] == ["refund-0", "refund-1"]


async def test_crash_during_non_idempotent_tool_is_reported_interrupted(db):
    crash_in.add("refund")
    with pytest.raises(SimulatedCrash):
        await agent(db, Scripted(calls(("refund", {"order_id": "42"}))), approvals=False).run("refund", run_id="c1")
    stored = await SQLiteRunStore(db).load("c1")
    assert stored.status == "running" and stored.pending.started == ["refund-0"]

    crash_in.clear()
    provider = Scripted(final("Checked."))
    resumed = agent(db, provider, approvals=False)
    await resumed.resume("c1")
    assert executed == ["refund:42"]
    assert tool_messages(provider)[0].content == "Error: Tool call interrupted before completion; not retried"
    assert "tool_interrupted" in event_types(resumed)


async def test_crash_during_idempotent_tool_reruns_it(db):
    crash_in.add("lookup")
    with pytest.raises(SimulatedCrash):
        await agent(db, Scripted(calls(("lookup", {"order_id": "7"}))), approvals=False).run("look", run_id="c1")
    crash_in.clear()
    provider = Scripted(final())
    await agent(db, provider, approvals=False).resume("c1")
    assert executed == ["lookup:7", "lookup:7"]
    assert tool_messages(provider)[0].content == '{"order_id": "7", "status": "paid"}'


async def test_crash_before_tool_started_runs_it_on_resume(db):
    def crashing_hook(ctx: Any) -> None:
        raise SimulatedCrash

    first = Agent(Scripted(calls(("refund", {"order_id": "9"}))), tools=[refund], config=AgentConfig(
        run_store=SQLiteRunStore(db), hooks=Hooks(before_tool=[crashing_hook])))
    with pytest.raises(SimulatedCrash):
        await first.run("refund", run_id="c1")
    assert (await SQLiteRunStore(db).load("c1")).pending.started == []

    provider = Scripted(final())
    await agent(db, provider, approvals=False).resume("c1")
    assert executed == ["refund:9"]
    assert tool_messages(provider)[0].content == '{"refunded": "9"}'


async def test_crash_in_model_call_after_tools_repeats_only_the_model_call(db):
    provider = Scripted(calls(("lookup", {"order_id": "1"})), SimulatedCrash())
    with pytest.raises(SimulatedCrash):
        await agent(db, provider, approvals=False).run("look", run_id="c1")
    resumed_provider = Scripted(final("paid"))
    done = await agent(db, resumed_provider, approvals=False).resume("c1")
    assert executed == ["lookup:1"] and done.output == "paid"
    assert len(resumed_provider.requests) == 1


async def test_failure_is_recorded_and_resume_retries(db):
    with pytest.raises(RuntimeError):
        await agent(db, Scripted(calls(("lookup", {"order_id": "1"})), RuntimeError("upstream")), approvals=False).run(
            "look", run_id="f1")
    stored = await SQLiteRunStore(db).load("f1")
    assert (stored.status, stored.error) == ("failed", "RuntimeError: upstream")
    done = await agent(db, Scripted(final("ok")), approvals=False).resume("f1")
    assert done.output == "ok" and executed == ["lookup:1"]


async def test_concurrent_resumes_execute_the_tool_once(db):
    await agent(db, Scripted(calls(("refund", {"order_id": "42"})))).run("refund", run_id="t1")
    outcomes = await asyncio.gather(
        agent(db, Scripted(final())).resume("t1", approvals={"refund-0": True}),
        agent(db, Scripted(final())).resume("t1", approvals={"refund-0": True}),
        return_exceptions=True,
    )
    assert sum(isinstance(o, RunConflictError) for o in outcomes) == 1
    assert executed == ["refund:42"]


async def test_resuming_a_completed_run_returns_the_stored_result(db):
    done = await agent(db, Scripted(final("all done")), approvals=False).run("hi", run_id="d1")
    again = await agent(db, Scripted(), approvals=False).resume("d1")
    assert (again.output, again.run_id, again.status) == ("all done", "d1", "completed")
    assert again.total_cost_usd == done.total_cost_usd


class Refund(BaseModel):
    order_id: str
    refunded: bool


async def test_typed_output_across_suspension(db):
    await agent(db, Scripted(calls(("refund", {"order_id": "42"})))).run("refund", run_id="t1", output_type=Refund)
    with pytest.raises(CheckpointError, match="output_type"):
        await agent(db, Scripted()).resume("t1", approvals={"refund-0": True})
    done = await agent(db, Scripted(final('{"order_id": "42", "refunded": true}'))).resume(
        "t1", approvals={"refund-0": True}, output_type=Refund)
    assert done.parsed == Refund(order_id="42", refunded=True)


async def test_resume_stream_parity(db):
    await agent(db, Scripted(calls(("refund", {"order_id": "42"})))).run("refund", run_id="t1")
    resumed = agent(db, Scripted(final("streamed")))
    chunks = [c async for c in resumed.resume_stream("t1", approvals={"refund-0": True})]
    assert chunks == ["streamed"]
    assert resumed.last_result is not None and resumed.last_result.status == "completed"


async def test_stream_suspends(db):
    a = agent(db, Scripted(calls(("refund", {"order_id": "42"}))))
    assert [c async for c in a.stream("refund", run_id="s1")] == []
    assert a.last_result is not None and a.last_result.status == "suspended"


async def test_guards(db, tmp_path):
    a = agent(db, Scripted(final(), final()), approvals=False)
    await a.run("hi", run_id="dup")
    with pytest.raises(ValueError, match="already exists; use agent.resume"):
        await a.run("hi", run_id="dup")
    with pytest.raises(TypeError, match="JSON-serialisable"):
        await a.run("hi", tenant=object())
    with pytest.raises(RunNotFoundError):
        await a.resume("missing")
    with pytest.raises(ValueError, match="run_store"):
        Agent(Scripted(), config=AgentConfig(approver=SUSPEND))
    with pytest.raises(ValueError, match="run_store"):
        await Agent(Scripted()).resume("x")
    plain = await Agent(Scripted(final())).run("hi", run_id="mine")
    assert plain.run_id == "mine"
