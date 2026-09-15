"""Agents as tools: delegation to child runs under the parent run's policy, budget, and durability."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from pydantic import BaseModel

from agent_kit import SUSPEND, Agent, AgentConfig, tool
from agent_kit.agent.delegation import AgentTool, DelegationContext, child_run_id
from agent_kit.durable import SQLiteRunStore
from agent_kit.exceptions import BudgetExceededError, RunConflictError, RunStoppedByHookError
from agent_kit.hooks import Decision, Hooks, require_approval
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


def calls(*specs: tuple[str, dict[str, Any]], cost: float = 0.01) -> Turn:
    tcs = [ToolCall(tool_name=n, arguments=a, call_id=f"{n}-{i}") for i, (n, a) in enumerate(specs)]
    return Turn(message_out=Message(role="assistant", content="", tool_calls=tcs), tool_calls=tcs,
                cost=CostSummary(total_tokens=10, cost_usd=cost))


def final(text: str = "done", cost: float = 0.01) -> Turn:
    return Turn(message_out=Message(role="assistant", content=text), cost=CostSummary(total_tokens=5, cost_usd=cost))


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
    return {"order_id": order_id, "status": "paid"}


@tool(description="do nothing", idempotent=True)
async def noop() -> dict[str, Any]:
    return {}


@pytest.fixture(autouse=True)
def _reset():
    executed.clear()
    crash_in.clear()


@pytest.fixture
def db(tmp_path):
    return tmp_path / "runs.db"


NO_RETRY = RetryPolicyConfig(max_attempts=1)


def refunds_agent(provider: Scripted, **config: Any) -> Agent:
    """A child whose `refund` tool is gated by its own approval hook."""
    return Agent(provider, tools=[refund, lookup], config=AgentConfig(
        hooks=Hooks(before_tool=[require_approval("refund")]), retry_policy=NO_RETRY, **config))


def make_ctx(**update: Any) -> DelegationContext:
    values: dict[str, Any] = dict(
        parent_run_id="parent-1", call_id="refunds-0", depth=1, max_depth=5, context={}, hooks=None,
        approver=None, approval_timeout_s=300.0, run_store=None, remaining_cost_usd=None, budget_guard=None,
        reporter=None, approvals={},
    )
    values.update(update)
    return DelegationContext(**values)


def tool_messages(provider: Scripted) -> list[Message]:
    return [m for m in provider.requests[-1] if m.role == "tool"]


class Verdict(BaseModel):
    approved: bool


# --- AgentTool --------------------------------------------------------------------------------------


def test_as_tool_schema_and_validation():
    child = Agent(Scripted())
    research = child.as_tool("research", "  Investigate a question.  ")
    assert isinstance(research, AgentTool) and research.agent is child
    assert research.schema.name == "research"
    assert research.schema.description == "Investigate a question."
    assert research.schema.parameters == {
        "type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"],
    }
    with pytest.raises(ValueError, match="agent tool name must match"):
        child.as_tool("bad name", "x")
    with pytest.raises(ValueError, match="description must not be empty"):
        child.as_tool("ok", "   ")


def test_child_run_id_is_a_stable_uuid():
    first = child_run_id("run-1", "call-1")
    assert first == child_run_id("run-1", "call-1") != child_run_id("run-1", "call-2")
    assert len(first) == 36 and str(uuid.UUID(first)) == first


async def test_standalone_calls_run_fresh_children():
    provider = Scripted(final("found it"), final("again"))
    child = Agent(provider)
    research = child.as_tool("research", "Investigate.")

    first = await research(task="q1")
    second = await research(task="q2")

    assert (first.output, second.output, first.error) == ("found it", "again", None)
    assert [m.content for m in provider.requests[1]] == ["q2"]
    assert len(child.memory) == 0
    assert child.audit is not None and child.audit.events() == []


async def test_standalone_typed_output_and_suspension(db):
    review = Agent(Scripted(final('{"approved": true}'))).as_tool("review", "Review.", output_type=Verdict)
    assert (await review(task="check")).output == {"approved": True}

    gated = refunds_agent(Scripted(calls(("refund", {"order_id": "1"}))), run_store=SQLiteRunStore(db), approver=SUSPEND)
    result = await gated.as_tool("refunds", "Refunds.")(task="refund 1")
    assert result.error == "delegated agent suspended; run it as a tool inside an agent to resume approvals"


async def test_depth_limit_starts_no_run():
    provider = Scripted()
    delegation = await Agent(provider).as_tool("research", "Investigate.").delegate("q", make_ctx(depth=6))
    assert (delegation.status, delegation.run_id, provider.requests) == ("failed", None, [])
    assert delegation.result is not None
    assert delegation.result.error == "delegation depth limit (5) exceeded"


async def test_child_hooks_run_before_parent_hooks():
    order: list[str] = []
    child = Agent(Scripted(calls(("noop", {})), final()), tools=[noop], config=AgentConfig(
        hooks=Hooks(before_tool=[lambda ctx: order.append("child")])))
    parent_hooks = Hooks(before_tool=[lambda ctx: order.append("parent")])

    delegation = await child.as_tool("worker", "Work.").delegate("go", make_ctx(hooks=parent_hooks))

    assert delegation.status == "completed" and order == ["child", "parent"]


async def test_parent_stop_propagates():
    stop = Hooks(before_llm=[lambda ctx: Decision.deny("after hours")])
    with pytest.raises(RunStoppedByHookError):
        await Agent(Scripted(final())).as_tool("worker", "Work.").delegate("go", make_ctx(hooks=stop))


async def test_child_failure_becomes_a_tool_error_with_its_spend():
    failing = Agent(Scripted(calls(("noop", {})), RuntimeError("upstream")), tools=[noop],
                    config=AgentConfig(retry_policy=NO_RETRY))
    delegation = await failing.as_tool("worker", "Work.").delegate("go", make_ctx())
    assert delegation.status == "failed"
    assert delegation.result is not None
    assert delegation.result.error == "delegated agent failed: RuntimeError: upstream"
    assert delegation.cost_usd == pytest.approx(0.01) == delegation.result.cost_usd


async def test_budget_errors_depend_on_whose_cap_binds():
    def capped_child(cap: float | None) -> AgentTool:
        return Agent(Scripted(calls(("noop", {})), final()), tools=[noop],
                     config=AgentConfig(max_run_cost_usd=cap)).as_tool("worker", "Work.")

    own = await capped_child(0.005).delegate("go", make_ctx())
    assert own.status == "failed" and own.result is not None
    assert own.result.error is not None and own.result.error.startswith("delegated agent failed: BudgetExceededError")

    with pytest.raises(BudgetExceededError):
        await capped_child(None).delegate("go", make_ctx(remaining_cost_usd=0.004))
    with pytest.raises(BudgetExceededError):
        await capped_child(0.5).delegate("go", make_ctx(remaining_cost_usd=0.004))


async def test_suspend_resume_and_replay_through_delegate(db):
    store = SQLiteRunStore(db)
    first = await refunds_agent(Scripted(calls(("refund", {"order_id": "1"})))).as_tool("refunds", "Refunds.").delegate(
        "refund 1", make_ctx(approver=SUSPEND, run_store=store))

    run_id = child_run_id("parent-1", "refunds-0")
    assert (first.status, first.run_id, first.result) == ("suspended", run_id, None)
    assert [(a.call_id, a.tool_name, a.run_id) for a in first.approvals] == [("refunds-0/refund-0", "refund", run_id)]
    stored = await store.load(run_id)
    assert stored is not None
    assert (stored.status, stored.parent_run_id, stored.parent_call_id) == ("suspended", "parent-1", "refunds-0")

    done = await refunds_agent(Scripted(final("refunded 1"))).as_tool("refunds", "Refunds.").delegate(
        "refund 1", make_ctx(approver=SUSPEND, run_store=store, approvals={"refund-0": True}))
    assert (done.status, executed) == ("completed", ["refund:1"])
    assert done.result is not None and done.result.output == "refunded 1"
    assert done.root_hash is not None and done.cost_usd == pytest.approx(0.02)

    replay = await Agent(Scripted()).as_tool("refunds", "Refunds.").delegate("refund 1", make_ctx(run_store=store))
    assert replay.result is not None and replay.result.output == "refunded 1"
    assert (replay.root_hash, replay.cost_usd, executed) == (done.root_hash, done.cost_usd, ["refund:1"])


async def test_concurrent_resumes_of_one_child_conflict(db):
    store = SQLiteRunStore(db)
    await refunds_agent(Scripted(calls(("refund", {"order_id": "1"})))).as_tool("refunds", "Refunds.").delegate(
        "refund 1", make_ctx(approver=SUSPEND, run_store=store))

    def resume() -> Any:
        return refunds_agent(Scripted(final())).as_tool("refunds", "Refunds.").delegate(
            "refund 1", make_ctx(approver=SUSPEND, run_store=SQLiteRunStore(db), approvals={"refund-0": True}))

    outcomes = await asyncio.gather(resume(), resume(), return_exceptions=True)
    assert sum(isinstance(o, RunConflictError) for o in outcomes) == 1
    assert executed == ["refund:1"]
