"""Agents as tools: delegation to child runs under the parent run's policy, budget, and durability."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from pydantic import BaseModel

from agent_kit import SUSPEND, Agent, AgentConfig, tool
from agent_kit.agent.delegation import AgentTool, DelegationContext, child_run_id
from agent_kit.cloud.models import CloudEvent
from agent_kit.cloud.reporter import CloudReporter
from agent_kit.durable import SQLiteRunStore
from agent_kit.exceptions import BudgetExceededError, RunConflictError, RunStoppedByHookError
from agent_kit.hooks import Decision, Hooks, deny_tools, require_approval
from agent_kit.providers.base import ProviderConfig
from agent_kit.types import AgentResult, CostSummary, Message, RetryPolicyConfig, ToolCall, Turn


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


# --- Delegation inside the loop --------------------------------------------------------------------


def lead(db, provider: Scripted, *children: AgentTool, approver: Any = SUSPEND, **config: Any) -> Agent:
    """A parent over a fresh store connection — stands in for another process."""
    return Agent(provider, tools=list(children), config=AgentConfig(
        run_store=SQLiteRunStore(db), approver=approver, retry_policy=NO_RETRY, **config))


def audit_spy(agent: Agent) -> list[tuple[str, dict[str, Any]]]:
    assert agent.audit is not None
    recorded: list[tuple[str, dict[str, Any]]] = []
    original = agent.audit.append

    def spy(event_type: str, actor: str, payload: dict[str, Any] | None = None) -> Any:
        recorded.append((event_type, payload or {}))
        return original(event_type, actor, payload)

    agent.audit.append = spy  # type: ignore[method-assign]
    return recorded


def recording_reporter(agent_name: str) -> tuple[CloudReporter, list[CloudEvent]]:
    reporter = CloudReporter(api_key="akt_test", project="proj", agent_name=agent_name)
    events: list[CloudEvent] = []

    async def enqueue(event: CloudEvent) -> None:
        events.append(event)

    reporter._enqueue = enqueue  # type: ignore[method-assign]
    return reporter, events


async def test_parallel_delegations_run_in_fresh_memory():
    child_provider = Scripted(final("a"), final("b"))
    child = Agent(child_provider)
    parent_provider = Scripted(calls(("research", {"task": "q1"}), ("research", {"task": "q2"})), final("summary"))

    result = await Agent(parent_provider, tools=[child.as_tool("research", "Investigate.")]).run("go")

    assert result.output == "summary"
    assert sorted([m.content for m in request] for request in child_provider.requests) == [["q1"], ["q2"]]
    assert sorted(m.content for m in tool_messages(parent_provider)) == ['"a"', '"b"']
    assert len(child.memory) == 0


async def test_typed_child_output_reaches_the_parent_as_json():
    review = Agent(Scripted(final('{"approved": true}'))).as_tool("review", "Review.", output_type=Verdict)
    parent_provider = Scripted(calls(("review", {"task": "check"})), final("ok"))
    await Agent(parent_provider, tools=[review]).run("go")
    assert tool_messages(parent_provider)[0].content == '{"approved": true}'


async def test_parent_policy_applies_inside_the_child():
    child_provider = Scripted(calls(("refund", {"order_id": "1"})), final("could not refund"))
    refunds = Agent(child_provider, tools=[refund]).as_tool("refunds", "Refunds.")
    parent = Agent(Scripted(calls(("refunds", {"task": "refund 1"})), final("ok")), tools=[refunds],
                   config=AgentConfig(hooks=Hooks(before_tool=[deny_tools("refund", reason="refunds frozen")])))

    await parent.run("go")

    assert executed == []
    assert [m.content for m in tool_messages(child_provider)] == ["Error: Tool call denied: refunds frozen"]


async def test_child_spend_rolls_into_the_parent_totals():
    child = Agent(Scripted(final("findings", cost=0.02)))
    parent = Agent(Scripted(calls(("research", {"task": "q"})), final("ok")), tools=[child.as_tool("research", "I.")])
    result = await parent.run("go")
    assert result.total_cost_usd == pytest.approx(0.04)
    assert result.total_tokens == 20


async def test_child_spend_trips_the_parent_cap_before_its_next_model_call():
    child = Agent(Scripted(final("findings", cost=0.02)))
    parent_provider = Scripted(calls(("research", {"task": "q"})), final("never"))
    parent = Agent(parent_provider, tools=[child.as_tool("research", "I.")], config=AgentConfig(max_run_cost_usd=0.025))

    with pytest.raises(BudgetExceededError) as exc:
        await parent.run("go")

    assert exc.value.spent_usd == pytest.approx(0.03)
    assert len(parent_provider.requests) == 1


async def test_delegation_is_audited_with_the_child_root_hash(db):
    research = Agent(Scripted(final("findings"))).as_tool("research", "Investigate.")
    parent = lead(db, Scripted(calls(("research", {"task": "q"})), final("ok")), research, approver=None)
    recorded = audit_spy(parent)

    await parent.run("go", run_id="lead-1")

    (payload,) = [p for event, p in recorded if event == "tool_call"]
    child = await SQLiteRunStore(db).load(child_run_id("lead-1", "research-0"))
    assert child is not None and child.result is not None
    assert payload["delegated_run_id"] == child.run_id
    assert payload["delegated_root_hash"] == child.result["audit_root_hash"]
    assert payload["delegated_cost_usd"] == pytest.approx(0.01)
    assert (child.parent_run_id, child.parent_call_id) == ("lead-1", "research-0")
    assert parent.audit is not None and parent.audit.verify()


async def test_child_reports_as_its_own_run_linked_to_the_parent():
    reporter, events = recording_reporter("lead")
    research = Agent(Scripted(final("findings"))).as_tool("research", "Investigate.")
    parent = Agent(Scripted(calls(("research", {"task": "q"})), final("ok")), tools=[research],
                   config=AgentConfig(cloud=reporter))

    result = await parent.run("go")

    assert result.run_id is not None
    child_id = child_run_id(result.run_id, "research-0")
    starts = {e.run_id: e.payload for e in events if e.event_type.value == "run_start"}
    assert starts[child_id]["parent_run_id"] == result.run_id
    assert "parent_run_id" not in starts[result.run_id]
    assert {e.run_id for e in events if e.event_type.value == "run_complete"} == {result.run_id, child_id}


async def test_stream_yields_only_the_parent_text():
    research = Agent(Scripted(final("child text"))).as_tool("research", "Investigate.")
    parent = Agent(Scripted(calls(("research", {"task": "q"})), final("summary")), tools=[research])
    assert [c async for c in parent.stream("go")] == ["summary"]


async def test_nested_delegation_and_depth_limit():
    def desk(max_depth: int) -> tuple[Agent, Scripted]:
        refunds = Agent(Scripted(final("refunded"))).as_tool("refunds", "Refunds.")
        desk_provider = Scripted(calls(("refunds", {"task": "refund 7"})), final("desk done"))
        desk_tool = Agent(desk_provider, tools=[refunds]).as_tool("desk", "Front desk.")
        parent_provider = Scripted(calls(("desk", {"task": "ticket"})), final("closed"))
        return Agent(parent_provider, tools=[desk_tool], config=AgentConfig(max_delegation_depth=max_depth)), desk_provider

    parent, _ = desk(5)
    result = await parent.run("go")
    assert result.output == "closed"
    assert result.total_cost_usd == pytest.approx(0.05)

    shallow, desk_provider = desk(1)
    await shallow.run("go")
    assert [m.content for m in tool_messages(desk_provider)] == ["Error: delegation depth limit (1) exceeded"]


async def ticket_run(db) -> tuple[Agent, AgentResult[Any]]:
    """Lead calls research (completes) and refunds (suspends on its refund approval) in one turn."""
    research = Agent(Scripted(final("findings", cost=0.02))).as_tool("research", "Investigate.")
    refunds = refunds_agent(Scripted(calls(("refund", {"order_id": "42"})))).as_tool("refunds", "Refunds.")
    first = lead(db, Scripted(calls(("research", {"task": "why"}), ("refunds", {"task": "refund 42"}))), research, refunds)
    return first, await first.run("handle ticket", run_id="lead-1")


async def test_suspended_child_suspends_the_parent(db):
    _, result = await ticket_run(db)

    child_id = child_run_id("lead-1", "refunds-1")
    assert (result.status, executed) == ("suspended", [])
    assert [(p.call_id, p.tool_name, p.arguments, p.run_id) for p in result.pending_approvals] == [
        ("refunds-1/refund-0", "refund", {"order_id": "42"}, child_id)
    ]
    assert result.total_cost_usd == pytest.approx(0.03)  # research 0.02 + the suspended child's 0.01
    stored = await SQLiteRunStore(db).load("lead-1")
    assert stored is not None and stored.pending is not None
    assert stored.status == "suspended" and set(stored.pending.results) == {"research-0"}
    assert stored.pending.delegated_cost_usd == {"research-0": pytest.approx(0.02), "refunds-1": pytest.approx(0.01)}
    child = await SQLiteRunStore(db).load(child_id)
    assert child is not None and child.status == "suspended"


# --- Resuming delegations --------------------------------------------------------------------------


def event_types(agent: Agent) -> list[str]:
    assert agent.audit is not None
    return [e.event_type for e in agent.audit.events()]


async def test_child_approval_resumes_from_another_process(db):
    await ticket_run(db)

    lead_provider = Scripted(final("ticket handled"))
    second = lead(
        db, lead_provider,
        Agent(Scripted()).as_tool("research", "Investigate."),  # already completed: any model call would fail
        refunds_agent(Scripted(final("refunded 42"))).as_tool("refunds", "Refunds."),
    )
    done = await second.resume("lead-1", approvals={"refunds-1/refund-0": True})

    assert (done.status, done.output) == ("completed", "ticket handled")
    assert executed == ["refund:42"]
    assert [m.content for m in tool_messages(lead_provider)] == ['"findings"', '"refunded 42"']
    assert done.total_cost_usd == pytest.approx(0.06)  # lead 0.02 + research 0.02 + refunds 0.02
    child = await SQLiteRunStore(db).load(child_run_id("lead-1", "refunds-1"))
    assert child is not None and child.status == "completed"
    assert second.audit is not None and second.audit.verify()


async def test_partial_child_answers_keep_both_runs_suspended(db):
    refunds = refunds_agent(Scripted(calls(("refund", {"order_id": "1"}), ("refund", {"order_id": "2"}))))
    await lead(db, Scripted(calls(("refunds", {"task": "two refunds"}))), refunds.as_tool("refunds", "R.")).run(
        "go", run_id="lead-1")

    idle = lead(db, Scripted(), refunds_agent(Scripted()).as_tool("refunds", "R."))
    partial = await idle.resume("lead-1", approvals={"refunds-0/refund-0": True})
    assert partial.status == "suspended"
    assert [p.call_id for p in partial.pending_approvals] == ["refunds-0/refund-1"]
    assert executed == ["refund:1"]

    lead_provider = Scripted(final("both refunded"))
    done = await lead(db, lead_provider, refunds_agent(Scripted(final("done"))).as_tool("refunds", "R.")).resume(
        "lead-1", approvals={"refunds-0/refund-1": True})
    assert done.output == "both refunded" and executed == ["refund:1", "refund:2"]


async def test_denied_child_approval_completes_the_child(db):
    await ticket_run(db)
    refunds_provider = Scripted(final("could not refund"))
    lead_provider = Scripted(final("told the customer"))
    done = await lead(db, lead_provider, Agent(Scripted()).as_tool("research", "I."),
                      refunds_agent(refunds_provider).as_tool("refunds", "R.")).resume(
        "lead-1", approvals={"refunds-1/refund-0": False})
    assert executed == []
    assert tool_messages(refunds_provider)[0].content == "Error: Tool call denied: approval denied"
    assert done.output == "told the customer"


async def test_two_level_nesting_routes_approvals_down(db):
    def desk(desk_provider: Scripted, refunds_provider: Scripted) -> AgentTool:
        refunds = refunds_agent(refunds_provider).as_tool("refunds", "Refunds.")
        return Agent(desk_provider, tools=[refunds], config=AgentConfig(retry_policy=NO_RETRY)).as_tool("desk", "Desk.")

    result = await lead(db, Scripted(calls(("desk", {"task": "ticket"}))),
                        desk(Scripted(calls(("refunds", {"task": "refund 7"}))), Scripted(calls(("refund", {"order_id": "7"}))))
                        ).run("go", run_id="lead-1")

    desk_id = child_run_id("lead-1", "desk-0")
    assert [(p.call_id, p.run_id) for p in result.pending_approvals] == [
        ("desk-0/refunds-0/refund-0", child_run_id(desk_id, "refunds-0"))
    ]

    done = await lead(db, Scripted(final("closed")), desk(Scripted(final("desk done")), Scripted(final("refunded")))
                      ).resume("lead-1", approvals={"desk-0/refunds-0/refund-0": True})
    assert (done.output, executed) == ("closed", ["refund:7"])


async def test_crash_inside_a_child_resumes_the_child(db):
    def orders(provider: Scripted) -> AgentTool:
        return Agent(provider, tools=[lookup], config=AgentConfig(retry_policy=NO_RETRY)).as_tool("orders", "Orders.")

    with pytest.raises(SimulatedCrash):
        await lead(db, Scripted(calls(("orders", {"task": "check 5"}))),
                   orders(Scripted(calls(("lookup", {"order_id": "5"})), SimulatedCrash())), approver=None).run(
            "go", run_id="lead-1")
    stored = await SQLiteRunStore(db).load("lead-1")
    assert stored is not None and stored.pending is not None and stored.pending.started == ["orders-0"]

    lead_provider = Scripted(final("order 5 is paid"))
    resumed = lead(db, lead_provider, orders(Scripted(final("paid"))), approver=None)
    done = await resumed.resume("lead-1")

    assert (done.output, executed) == ("order 5 is paid", ["lookup:5"])
    assert tool_messages(lead_provider)[0].content == '"paid"'
    assert "tool_interrupted" not in event_types(resumed)


async def test_completed_child_is_replayed_after_a_parent_crash(db):
    def crash(ctx: Any) -> None:
        raise SimulatedCrash

    with pytest.raises(SimulatedCrash):
        await lead(db, Scripted(calls(("research", {"task": "q"}))),
                   Agent(Scripted(final("findings"))).as_tool("research", "I."),
                   approver=None, hooks=Hooks(after_tool=[crash])).run("go", run_id="lead-1")

    lead_provider = Scripted(final("ok"))
    await lead(db, lead_provider, Agent(Scripted()).as_tool("research", "I."), approver=None).resume("lead-1")
    assert tool_messages(lead_provider)[0].content == '"findings"'


async def test_crash_before_the_child_started_runs_it_on_resume(db, monkeypatch):
    async def crash(self: AgentTool, task: str, ctx: DelegationContext) -> Any:
        raise SimulatedCrash

    monkeypatch.setattr(AgentTool, "delegate", crash)
    with pytest.raises(SimulatedCrash):
        await lead(db, Scripted(calls(("research", {"task": "q"}))), Agent(Scripted()).as_tool("research", "I."),
                   approver=None).run("go", run_id="lead-1")
    monkeypatch.undo()
    assert await SQLiteRunStore(db).load(child_run_id("lead-1", "research-0")) is None

    lead_provider = Scripted(final("ok"))
    await lead(db, lead_provider, Agent(Scripted(final("findings"))).as_tool("research", "I."), approver=None).resume(
        "lead-1")
    assert tool_messages(lead_provider)[0].content == '"findings"'
