# Agent as Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `agent.as_tool(name, description, output_type=None)` turns an `Agent` into a tool whose every call is a fresh child run that inherits the parent run's hooks, approver, run store, and remaining cost cap; child approvals bubble up into the parent's `pending_approvals` and resume through `parent.resume(...)`; child spend rolls into the parent; the parent's audit chain commits to each child's root hash.

**Architecture:** A new `agent_kit/agent/delegation.py` holds `AgentTool(Tool)`, `DelegationContext`, `Delegation`, `child_run_id`, and `stack_hooks`. `AgentTool.delegate()` builds a child `AgentLoop` through `Agent._open_delegated()` (fresh memory and audit, restored from the child checkpoint when one exists) with run-scoped overrides, then starts, resumes, or replays the child and classifies errors. `AgentLoop._run_tool` sends `AgentTool` calls to a new `_delegate()`, which adds spend to the run, and parks a suspended child's approvals under the call; `_resolve_tools` routes prefixed approval answers to the child and resumes started delegations instead of reporting them interrupted.

**Tech Stack:** Python 3.11+, Pydantic v2, sqlite3 (`SQLiteRunStore`), pytest-asyncio (`asyncio_mode = "auto"`), mypy strict, ruff.

**Spec:** `specs/16-agent-as-tool.md`

## Global Constraints

- `agent_kit/types.py` imports nothing from `agent_kit`. `agent_kit/agent/delegation.py` must not import `agent_kit.agent.agent` or `agent_kit.agent.loop` at runtime (`TYPE_CHECKING` only) — `loop.py` imports it.
- Child run id: `str(uuid.uuid5(DELEGATION_NAMESPACE, f"{parent_run_id}/{call_id}"))` with `DELEGATION_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/maco144/agent-kit/delegation")`.
- Bubbled approval ids: `f"{call_id}/{child_call_id}"`, prefixed again at each level.
- Agent tool input schema: `{"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}`; name regex `^[A-Za-z0-9_-]{1,64}$`.
- `AgentConfig.max_delegation_depth = 5`; top-level run depth 0, its children 1; `depth > max_depth` fails without starting a run.
- New fields all have defaults; `CHECKPOINT_SCHEMA_VERSION` stays `1`.
- Messages (exact): `"delegation depth limit (<max>) exceeded"`, `"delegated agent failed: <ExceptionType>: <message>"`, `"delegated agent suspended; run it as a tool inside an agent to resume approvals"`, `"run_id must be at most 36 characters when reporting to agent-kit Cloud"`, `"agent tool name must match ^[A-Za-z0-9_-]{1,64}$, got <repr>"`, `"agent tool description must not be empty"`.
- Re-raised from a child: `RunStoppedByHookError`, `RunConflictError`, `CheckpointError`, `BudgetExceededError` when `scope != "run"` or the parent's remaining cap was the binding limit. Everything else becomes the tool error.
- Tests simulate crashes with a `BaseException` subclass (never `KeyboardInterrupt`). Scripted tool call ids are `f"{name}-{index}"` and unique within a turn only — tests that use a run store never call the same agent tool from two turns of one run.
- Gates per task: `python3 -m pytest -q`, `ruff check agent_kit tests`, `python3 -m mypy agent_kit`. The server suite (`cd server && python3 -m pytest -q`) runs once in Task 6 — no task touches `server/`.

---

### Task 1: Delegation fields, `parent_run_id` on `run_start`, run id length check

**Files:**
- Modify: `agent_kit/types.py` (`ToolResult`, `PendingApproval`)
- Modify: `agent_kit/durable/models.py` (`PendingTurn`, `RunCheckpoint`)
- Modify: `agent_kit/cloud/reporter.py` (`on_run_start`)
- Modify: `agent_kit/agent/agent.py` (`run`, `stream`)
- Test: `tests/test_run_store.py`, `tests/test_cloud_reporter.py`, `tests/test_agent.py`

**Interfaces:**
- Produces: `ToolResult.cost_usd: float = 0.0`, `ToolResult.tokens: int = 0`; `PendingApproval.run_id: str | None = None`; `PendingTurn.delegated_cost_usd: dict[str, float]`, `PendingTurn.delegated_tokens: dict[str, int]`, `PendingTurn.delegated_root_hash: dict[str, str]`; `RunCheckpoint.parent_run_id: str | None`, `RunCheckpoint.parent_call_id: str | None`; `CloudReporter.on_run_start(run_id, model, prompt, parent_run_id=None)`.

- [ ] **Step 1: Write the failing tests**

`tests/test_run_store.py` (append; `checkpoint()` is the existing helper in that file):

```python
def test_checkpoints_without_delegation_fields_still_load():
    data = checkpoint().model_dump(mode="json")
    for key in ("parent_run_id", "parent_call_id"):
        data.pop(key)
    for key in ("delegated_cost_usd", "delegated_tokens", "delegated_root_hash"):
        data["pending"].pop(key)
    for approval in data["pending"]["approvals"]:
        approval.pop("run_id")
    for result in data["pending"]["results"].values():
        result.pop("cost_usd")
        result.pop("tokens")

    cp = RunCheckpoint.model_validate(data)

    assert (cp.parent_run_id, cp.parent_call_id) == (None, None)
    assert cp.pending is not None
    assert (cp.pending.delegated_cost_usd, cp.pending.delegated_tokens, cp.pending.delegated_root_hash) == ({}, {}, {})
    assert cp.pending.approvals[0].run_id is None
    assert (cp.pending.results["c0"].cost_usd, cp.pending.results["c0"].tokens) == (0.0, 0)
```

`tests/test_cloud_reporter.py` (append; `make_reporter()` exists in that file):

```python
async def test_run_start_carries_parent_run_id_only_when_set():
    reporter = make_reporter()
    await reporter.on_run_start(run_id="child", model=None, prompt="hi", parent_run_id="parent")
    await reporter.on_run_start(run_id="top", model=None, prompt="hi")
    child, top = reporter._queue.get_nowait(), reporter._queue.get_nowait()
    assert child.payload["parent_run_id"] == "parent"
    assert "parent_run_id" not in top.payload
```

`tests/test_agent.py` (append):

```python
async def test_long_run_id_rejected_when_reporting_to_cloud(mock_provider):
    from agent_kit.cloud.reporter import CloudReporter

    reporting = Agent(mock_provider, config=AgentConfig(cloud=CloudReporter(api_key="akt_test", project="p")))
    with pytest.raises(ValueError, match="at most 36 characters"):
        await reporting.run("hi", run_id="x" * 37)
    with pytest.raises(ValueError, match="at most 36 characters"):
        [c async for c in reporting.stream("hi", run_id="x" * 37)]
    assert (await reporting.run("hi", run_id="x" * 36)).run_id == "x" * 36
    assert (await Agent(mock_provider).run("hi", run_id="x" * 37)).run_id == "x" * 37
```

Confirm `tests/test_agent.py` already imports `AgentConfig` and `pytest`; add them to its imports if not.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_run_store.py::test_checkpoints_without_delegation_fields_still_load tests/test_cloud_reporter.py::test_run_start_carries_parent_run_id_only_when_set tests/test_agent.py::test_long_run_id_rejected_when_reporting_to_cloud -v`
Expected: FAIL — `KeyError: 'parent_run_id'` (pop of a missing key), `TypeError: ... unexpected keyword argument 'parent_run_id'`, `DID NOT RAISE ValueError`.

- [ ] **Step 3: Implement**

`agent_kit/types.py` — `ToolResult` gains, after `idempotency_key`:

```python
    cost_usd: float = 0.0  # spend incurred inside the tool (delegated agent runs)
    tokens: int = 0
```

`PendingApproval` gains, after `turn`:

```python
    run_id: str | None = None  # run that owns the gated call; None for the run's own calls
```

`agent_kit/durable/models.py` — `PendingTurn` gains, after `approvals`:

```python
    delegated_cost_usd: dict[str, float] = Field(default_factory=dict)  # call_id → child spend added to the run
    delegated_tokens: dict[str, int] = Field(default_factory=dict)
    delegated_root_hash: dict[str, str] = Field(default_factory=dict)  # call_id → completed child's audit root
```

`RunCheckpoint` gains, after `error`:

```python
    parent_run_id: str | None = None  # set on delegated (child) runs
    parent_call_id: str | None = None
```

`agent_kit/cloud/reporter.py` — replace `on_run_start`:

```python
    async def on_run_start(
        self, run_id: str, model: str | None, prompt: str, parent_run_id: str | None = None
    ) -> None:
        payload: dict[str, Any] = {"model": model, "prompt_hash": _sha256(prompt)}
        if parent_run_id is not None:
            payload["parent_run_id"] = parent_run_id
        await self._enqueue(CloudEvent(
            event_type=EventType.RUN_START,
            run_id=run_id,
            agent_name=self._agent_name,
            project=self._project,
            payload=payload,
        ))
```

`agent_kit/agent/agent.py` — module constant after `T = TypeVar("T")`:

```python
_MAX_CLOUD_RUN_ID = 36  # agent-kit Cloud stores run ids as String(36) and addresses them in URL paths
```

Method on `Agent` (after `add_tool`):

```python
    def _check_run_id(self, run_id: str | None) -> None:
        if run_id is not None and self._config.cloud is not None and len(run_id) > _MAX_CLOUD_RUN_ID:
            raise ValueError("run_id must be at most 36 characters when reporting to agent-kit Cloud")
```

Call `self._check_run_id(run_id)` as the first line of the `run()` implementation (the non-overload body) and of `stream()`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: the Step 2 command. Expected: PASS. Then the gates: `python3 -m pytest -q`, `ruff check agent_kit tests`, `python3 -m mypy agent_kit`.

- [ ] **Step 5: Commit**

```bash
git add agent_kit/types.py agent_kit/durable/models.py agent_kit/cloud/reporter.py agent_kit/agent/agent.py tests/test_run_store.py tests/test_cloud_reporter.py tests/test_agent.py
git commit -m "feat: delegation fields on results and checkpoints, parent_run_id on run_start, cloud run id length check"
```

---

### Task 2: `AgentTool` and child runs

**Files:**
- Create: `agent_kit/agent/delegation.py`
- Modify: `agent_kit/agent/agent.py` (`AgentConfig.max_delegation_depth`, `Agent.config`, `as_tool`, `_make_loop(**overrides)`, `_open_delegated`, checkpoint helpers)
- Modify: `agent_kit/agent/loop.py` (constructor params, `_snapshot`, `on_run_start`, `spend()`)
- Test: `tests/test_agent_tool.py` (create)

**Interfaces:**
- Consumes (Task 1): `ToolResult.cost_usd/tokens`, `PendingApproval.run_id`, `RunCheckpoint.parent_run_id/parent_call_id`, `on_run_start(..., parent_run_id=)`.
- Produces:
  - `agent_kit.agent.delegation.DELEGATION_NAMESPACE: uuid.UUID`
  - `child_run_id(parent_run_id: str, call_id: str) -> str`
  - `stack_hooks(child: Hooks | None, parent: Hooks | None) -> Hooks | None`
  - `DelegationContext(parent_run_id, call_id, depth, max_depth, context, hooks, approver, approval_timeout_s, run_store, remaining_cost_usd, budget_guard, reporter, approvals)` (frozen dataclass)
  - `Delegation(run_id: str | None, status, result: ToolResult | None, approvals: list[PendingApproval], cost_usd: float, tokens: int, root_hash: str | None)` (frozen dataclass)
  - `AgentTool(agent, name, description, output_type=None)` with `.agent`, `.output_type`, `async delegate(task: str, ctx: DelegationContext) -> Delegation`
  - `Agent.as_tool(name, description, *, output_type=None) -> AgentTool`; `Agent.config -> AgentConfig`; `Agent._make_loop(**overrides) -> AgentLoop`; `Agent._open_delegated(run_id, output_type, **overrides) -> tuple[AgentLoop, RunCheckpoint | None]`
  - `AgentLoop(..., delegation_depth=0, max_delegation_depth=5, parent_run_id=None, parent_call_id=None)`; `AgentLoop.spend() -> tuple[float, int]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_agent_tool.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_agent_tool.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'agent_kit.agent.delegation'`.

- [ ] **Step 3: Implement `agent_kit/agent/delegation.py`**

```python
"""
Agents as tools — delegation to a child run under the parent run's policy, budget, and durability.

    research = Agent(provider, tools=[web_search]).as_tool("research", "Investigate a question.")
    lead = Agent(provider, tools=[research], config=AgentConfig(max_run_cost_usd=2.00))

Inside an agent loop every call is a fresh child run (see specs/16-agent-as-tool.md). The child keeps its own
provider, tools, and hooks; the parent run supplies its hooks (run after the child's), approver, run store, and
remaining cost cap. A suspended child suspends the parent with the child's approvals under prefixed call ids.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from agent_kit.exceptions import BudgetExceededError, CheckpointError, RunConflictError, RunStoppedByHookError
from agent_kit.hooks import Hooks
from agent_kit.output import OutputSpec
from agent_kit.tools.base import Tool
from agent_kit.types import AgentResult, PendingApproval, ToolResult, ToolSchema

if TYPE_CHECKING:
    from agent_kit.agent.agent import Agent
    from agent_kit.agent.loop import AgentLoop
    from agent_kit.cloud.budgets import BudgetGuard
    from agent_kit.cloud.reporter import CloudReporter
    from agent_kit.durable import RunStore
    from agent_kit.hooks import Approver, Suspend

DELEGATION_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/maco144/agent-kit/delegation")

_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TASK_SCHEMA = {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}


def child_run_id(parent_run_id: str, call_id: str) -> str:
    """Deterministic, 36-character run id for the child run of one delegated tool call."""
    return str(uuid.uuid5(DELEGATION_NAMESPACE, f"{parent_run_id}/{call_id}"))


def stack_hooks(child: Hooks | None, parent: Hooks | None) -> Hooks | None:
    """The child's hooks run first, then the parent's; any deny wins."""
    if child is None or parent is None:
        return child or parent
    return Hooks(
        before_tool=[*child.before_tool, *parent.before_tool],
        after_tool=[*child.after_tool, *parent.after_tool],
        before_llm=[*child.before_llm, *parent.before_llm],
    )


@dataclass(frozen=True)
class DelegationContext:
    """Parent run state handed to AgentTool.delegate()."""

    parent_run_id: str
    call_id: str
    depth: int  # depth of the child run (top-level run = 0, its children = 1)
    max_depth: int
    context: dict[str, Any]  # parent run context
    hooks: Hooks | None  # parent's effective (already stacked) hooks
    approver: Approver | Suspend | None
    approval_timeout_s: float
    run_store: RunStore | None
    remaining_cost_usd: float | None  # parent cap minus parent spend; None = uncapped
    budget_guard: BudgetGuard | None
    reporter: CloudReporter | None
    approvals: dict[str, bool]  # answers for this child, prefix stripped


@dataclass(frozen=True)
class Delegation:
    """Outcome of one delegated call, as the parent loop sees it."""

    run_id: str | None  # None when no child run started (depth limit)
    status: Literal["completed", "suspended", "failed"]
    result: ToolResult | None  # completed or failed
    approvals: list[PendingApproval]  # suspended: the child's pending approvals, ids already prefixed
    cost_usd: float  # child's cumulative spend, every level below included
    tokens: int
    root_hash: str | None  # child's audit root hash when completed


class AgentTool(Tool):
    """An Agent exposed as a tool. Build with ``Agent.as_tool()``."""

    def __init__(self, agent: Agent, name: str, description: str, output_type: Any = None) -> None:
        if not _NAME.match(name):
            raise ValueError(f"agent tool name must match ^[A-Za-z0-9_-]{{1,64}}$, got {name!r}")
        if not description.strip():
            raise ValueError("agent tool description must not be empty")
        schema = ToolSchema(name=name, description=description.strip(), parameters=dict(_TASK_SCHEMA))
        super().__init__(self._standalone, schema)
        self.agent = agent
        self.output_type = output_type

    async def delegate(self, task: str, ctx: DelegationContext) -> Delegation:
        """Start, resume, or replay this call's child run under the parent run's policy and budget."""
        t0 = time.monotonic()
        if ctx.depth > ctx.max_depth:
            error = f"delegation depth limit ({ctx.max_depth}) exceeded"
            return Delegation(None, "failed", self._result(ctx, t0, error=error), [], 0.0, 0, None)

        run_id = child_run_id(ctx.parent_run_id, ctx.call_id)
        loop, checkpoint = await self.agent._open_delegated(run_id, self.output_type, **self._overrides(ctx))
        try:
            if checkpoint is not None and checkpoint.status == "completed":
                stored = self.agent._stored_result(checkpoint, self.output_type)
                return self._completed(ctx, t0, run_id, stored, stored.total_cost_usd, stored.total_tokens)
            if checkpoint is not None:
                result = await loop.resume(checkpoint, ctx.approvals, self.output_type)
            else:
                context = {**ctx.context, "parent_run_id": ctx.parent_run_id}
                result = await loop.run(task, output_type=self.output_type, run_id=run_id, **context)
        except (RunStoppedByHookError, RunConflictError, CheckpointError):
            raise
        except BudgetExceededError as exc:
            if exc.scope != "run" or self._parent_bound(ctx):
                raise
            return self._failed(ctx, t0, run_id, loop, exc)
        except Exception as exc:
            return self._failed(ctx, t0, run_id, loop, exc)

        cost, tokens = loop.spend()
        if result.status == "suspended":
            approvals = [
                a.model_copy(update={"call_id": f"{ctx.call_id}/{a.call_id}", "run_id": a.run_id or run_id})
                for a in result.pending_approvals
            ]
            return Delegation(run_id, "suspended", None, approvals, cost, tokens, None)
        return self._completed(ctx, t0, run_id, result, cost, tokens)

    async def _standalone(self, task: str) -> Any:
        """Outside an agent loop: run the child once with its own full config."""
        run_id = str(uuid.uuid4())
        loop, _ = await self.agent._open_delegated(run_id, self.output_type)
        result = await loop.run(task, output_type=self.output_type, run_id=run_id)
        if result.status == "suspended":
            raise RuntimeError("delegated agent suspended; run it as a tool inside an agent to resume approvals")
        return self._output(result)

    def _overrides(self, ctx: DelegationContext) -> dict[str, Any]:
        config = self.agent.config
        caps = [c for c in (config.max_run_cost_usd, ctx.remaining_cost_usd) if c is not None]
        overrides: dict[str, Any] = {
            "hooks": stack_hooks(config.hooks, ctx.hooks),
            "approver": ctx.approver,
            "approval_timeout_s": ctx.approval_timeout_s,
            "run_store": ctx.run_store,
            "max_run_cost_usd": min(caps) if caps else None,
            "delegation_depth": ctx.depth,
            "max_delegation_depth": ctx.max_depth,
            "parent_run_id": ctx.parent_run_id,
            "parent_call_id": ctx.call_id,
        }
        if not config.enforce_budgets:
            overrides["budget_guard"] = ctx.budget_guard
        if config.cloud is None:
            overrides["reporter"] = ctx.reporter
        return overrides

    def _parent_bound(self, ctx: DelegationContext) -> bool:
        own = self.agent.config.max_run_cost_usd
        return ctx.remaining_cost_usd is not None and (own is None or ctx.remaining_cost_usd <= own)

    def _output(self, result: AgentResult[Any]) -> Any:
        if self.output_type is None:
            return result.output
        return OutputSpec.from_type(self.output_type).adapter.dump_python(result.parsed, mode="json")

    def _completed(
        self, ctx: DelegationContext, t0: float, run_id: str, result: AgentResult[Any], cost: float, tokens: int
    ) -> Delegation:
        tool_result = self._result(ctx, t0, output=self._output(result), cost_usd=cost, tokens=tokens)
        return Delegation(run_id, "completed", tool_result, [], cost, tokens, result.audit_root_hash)

    def _failed(self, ctx: DelegationContext, t0: float, run_id: str, loop: AgentLoop, exc: Exception) -> Delegation:
        cost, tokens = loop.spend()
        error = f"delegated agent failed: {type(exc).__name__}: {exc}"
        tool_result = self._result(ctx, t0, error=error, cost_usd=cost, tokens=tokens)
        return Delegation(run_id, "failed", tool_result, [], cost, tokens, None)

    def _result(
        self,
        ctx: DelegationContext,
        t0: float,
        output: Any = None,
        error: str | None = None,
        cost_usd: float = 0.0,
        tokens: int = 0,
    ) -> ToolResult:
        return ToolResult(
            call_id=ctx.call_id,
            tool_name=self.schema.name,
            output=output,
            error=error,
            duration_ms=int((time.monotonic() - t0) * 1000),
            cost_usd=cost_usd,
            tokens=tokens,
        )

    def __repr__(self) -> str:
        return f"AgentTool(name={self.schema.name!r}, agent={self.agent!r})"
```

- [ ] **Step 4: Implement the `AgentLoop` changes** (`agent_kit/agent/loop.py`)

Constructor — add parameters after `run_store`:

```python
        delegation_depth: int = 0,
        max_delegation_depth: int = 5,
        parent_run_id: str | None = None,
        parent_call_id: str | None = None,
```

and store them after `self._checkpointer = ...`:

```python
        self._delegation_depth = delegation_depth  # 0 for a top-level run
        self._max_delegation_depth = max_delegation_depth
        self._parent_run_id = parent_run_id  # set on delegated (child) runs
        self._parent_call_id = parent_call_id
```

`_execute` — the reporter call becomes:

```python
        if self._reporter and restore is None:
            await self._reporter.on_run_start(
                run_id=run_id,
                model=self._model or self._provider.config.default_model,
                prompt=prompt,
                parent_run_id=self._parent_run_id,
            )
```

`_snapshot` — add to the `RunCheckpoint(...)` call:

```python
            parent_run_id=self._parent_run_id,
            parent_call_id=self._parent_call_id,
```

New method after `_totals`:

```python
    def spend(self) -> tuple[float, int]:
        """This run's cost and tokens so far, delegated runs included — also for a run that raised."""
        return self._run_cost_usd, self._totals()[1]
```

- [ ] **Step 5: Implement the `Agent` changes** (`agent_kit/agent/agent.py`)

Import after `from agent_kit.agent.loop import AgentLoop`:

```python
from agent_kit.agent.delegation import AgentTool
```

`AgentConfig.__init__` — parameter after `run_store`: `max_delegation_depth: int = 5,` and assignment `self.max_delegation_depth = max_delegation_depth  # nested agent-tool levels allowed below a top-level run`.

`Agent` — after `add_tool`:

```python
    @property
    def config(self) -> AgentConfig:
        return self._config

    def as_tool(self, name: str, description: str, *, output_type: Any = None) -> AgentTool:
        """
        Expose this agent as a tool another agent can delegate to.

        Each call inside an agent loop is a fresh child run with its own memory and audit chain. The child
        keeps its provider, tools, and hooks; the calling run adds its hooks (run after the child's), its
        approver and run store, and its remaining cost cap. The model passes ``task``; the tool returns the
        child's answer, or its ``parsed`` value as JSON when ``output_type`` is set.
        """
        return AgentTool(self, name, description, output_type)
```

Replace `_load_checkpoint` and `_restore` with these, and add the helpers:

```python
    async def _load_checkpoint(self, run_id: str) -> RunCheckpoint:
        if self._config.run_store is None:
            raise ValueError("resume() requires AgentConfig(run_store=...)")
        checkpoint = await self._config.run_store.load(run_id)
        if checkpoint is None:
            raise RunNotFoundError(run_id)
        self._check_schema(checkpoint)
        return checkpoint

    @staticmethod
    def _check_schema(checkpoint: RunCheckpoint) -> None:
        if checkpoint.schema_version > CHECKPOINT_SCHEMA_VERSION:
            raise CheckpointError(
                checkpoint.run_id,
                f"schema version {checkpoint.schema_version} is newer than {CHECKPOINT_SCHEMA_VERSION}",
            )

    @staticmethod
    def _check_output_type(checkpoint: RunCheckpoint, output_type: Any) -> None:
        name = OutputSpec.from_type(output_type).name if output_type is not None else None
        if name != checkpoint.output_type_name:
            raise CheckpointError(
                checkpoint.run_id,
                f"output_type {name!r} does not match the run's {checkpoint.output_type_name!r}",
            )

    @staticmethod
    def _restored_audit(checkpoint: RunCheckpoint) -> AuditChain:
        try:
            return AuditChain.restore(checkpoint.audit_events)
        except AuditVerificationError as exc:
            raise CheckpointError(checkpoint.run_id, f"audit chain failed verification: {exc}") from exc

    def _restore(self, checkpoint: RunCheckpoint, output_type: Any) -> None:
        self._check_output_type(checkpoint, output_type)
        if self._audit is not None:
            self._audit = self._restored_audit(checkpoint)
        self._memory.clear()
        self._memory.add_many(checkpoint.messages)

    async def _open_delegated(
        self, run_id: str, output_type: Any, **overrides: Any
    ) -> tuple[AgentLoop, RunCheckpoint | None]:
        """A loop for one delegated run over fresh memory and audit, restored from its checkpoint if unfinished."""
        memory = InMemoryStore(window=self._config.memory_window)
        audit = AuditChain() if self._config.audit_enabled else None
        store: RunStore | None = overrides.get("run_store", self._config.run_store)
        checkpoint = await store.load(run_id) if store is not None else None
        if checkpoint is not None:
            self._check_schema(checkpoint)
            if checkpoint.status != "completed":
                self._check_output_type(checkpoint, output_type)
                if audit is not None:
                    audit = self._restored_audit(checkpoint)
                memory.add_many(checkpoint.messages)
        return self._make_loop(memory=memory, audit=audit, **overrides), checkpoint
```

Replace `_make_loop` with a keyword dict that overrides can update (the default values are exactly today's arguments plus `max_delegation_depth`):

```python
    def _make_loop(self, **overrides: Any) -> AgentLoop:
        kwargs: dict[str, Any] = dict(
            provider=self._provider,
            registry=self._registry,
            memory=self._memory,
            tracer=self._tracer,
            audit=self._audit,
            model=self._config.model,
            system_prompt=self._config.system_prompt,
            max_turns=self._config.max_turns,
            max_tokens_per_turn=self._config.max_tokens_per_turn,
            retry_policy=self._config.retry_policy,
            circuit_breaker_config=self._config.circuit_breaker,
            reporter=self._config.cloud,
            max_run_cost_usd=self._config.max_run_cost_usd,
            budget_guard=(
                self._config.cloud.budget_guard()
                if self._config.enforce_budgets and self._config.cloud is not None
                else None
            ),
            hooks=self._config.hooks,
            approver=self._config.approver,
            approval_timeout_s=self._config.approval_timeout_s,
            output_retries=self._config.output_retries,
            request_options=RequestOptions(
                thinking=self._config.thinking,
                effort=self._config.effort,
                prompt_caching=self._config.prompt_caching,
                compaction=self._config.compaction,
                clear_tool_results=self._config.clear_tool_results,
                provider_options=dict(self._config.provider_options),
            ),
            context_budget_tokens=self._config.context_budget_tokens,
            run_store=self._config.run_store,
            max_delegation_depth=self._config.max_delegation_depth,
        )
        kwargs.update(overrides)
        return AgentLoop(**kwargs)
```

Export: `agent_kit/agent/__init__.py` becomes

```python
from agent_kit.agent.agent import Agent, AgentConfig
from agent_kit.agent.delegation import AgentTool

__all__ = ["Agent", "AgentConfig", "AgentTool"]
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_agent_tool.py -v`
Expected: PASS (11 tests). Then the gates: `python3 -m pytest -q` (durable, hooks, and typed suites exercise the refactored `_restore` / `_make_loop`), `ruff check agent_kit tests`, `python3 -m mypy agent_kit`.

- [ ] **Step 7: Commit**

```bash
git add agent_kit/agent/delegation.py agent_kit/agent/agent.py agent_kit/agent/loop.py agent_kit/agent/__init__.py tests/test_agent_tool.py
git commit -m "feat: AgentTool — agents as tools running fresh child runs under the parent run's policy"
```

---

### Task 3: Delegation in the agent loop — results, spend, audit, cloud, suspension bubbling

**Files:**
- Modify: `agent_kit/agent/loop.py` (`_run_tool`, new `_delegate` / `_add_delegated_spend`, `_totals`, `_record_tool_results`, `_mark_started`, `_suspended`, `_request_approval`, `_resolve_tools` parking + sort)
- Test: `tests/test_agent_tool.py` (append)

**Interfaces:**
- Consumes (Task 2): `AgentTool.delegate(task, DelegationContext) -> Delegation`, `child_run_id`, `DelegationContext`, `Delegation`, `AgentLoop` delegation params.
- Produces: `AgentLoop._run_tool(tc, turn, gate=True, approvals=None) -> ToolResult | None`; `AgentLoop._suspended: dict[str, list[PendingApproval]]`; parent `tool_call` audit payload keys `delegated_run_id`, `delegated_cost_usd`, `delegated_root_hash`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_agent_tool.py`)

Add to the imports at the top of the file:

```python
from agent_kit.cloud.models import CloudEvent
from agent_kit.cloud.reporter import CloudReporter
from agent_kit.hooks import deny_tools
from agent_kit.types import AgentResult
```

Append:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_agent_tool.py -v -k "parallel or typed_child or parent_policy or rolls or trips or audited or reports or stream_yields or nested or suspended_child"`
Expected: FAIL — agent tools run through `Tool.__call__` (standalone), so there is no policy stacking, no spend roll-up, no `delegated_*` audit keys, no `parent_run_id`, and a child suspension becomes a tool error.

- [ ] **Step 3: Implement** (`agent_kit/agent/loop.py`)

Import after the `agent_kit.audit.chain` import:

```python
from agent_kit.agent.delegation import AgentTool, Delegation, DelegationContext, child_run_id
```

Constructor — the parked-approvals map now holds a list per call:

```python
        self._suspended: dict[str, list[PendingApproval]] = {}  # calls parked by approver=SUSPEND this turn
```

`_request_approval` — the `Suspend` branch stores a list:

```python
        if isinstance(self._approver, Suspend):
            self._suspended[ctx.call_id] = [
                PendingApproval(
                    call_id=ctx.call_id,
                    tool_name=ctx.tool_name,
                    arguments=dict(ctx.arguments),
                    reason=reason,
                    turn=ctx.turn,
                )
            ]
            return None
```

`_resolve_tools` — two changes: park lists, and sort bubbled ids by their top-level call:

```python
            if result is None:
                pending.approvals.extend(self._suspended.pop(tc.call_id))
            else:
                pending.results[tc.call_id] = result

        await asyncio.gather(*(resolve(tc) for tc in pending.turn.tool_calls))
        pending.approvals.sort(key=lambda a: order[a.call_id.split("/", 1)[0]])
        return list(pending.approvals)
```

`_mark_started` — a resumed delegation is marked once:

```python
    async def _mark_started(self, call_id: str) -> None:
        if self._pending is not None:
            if call_id in self._pending.started:
                return
            self._pending.started.append(call_id)
        if self._checkpointer is not None:
            await self._checkpointer.mark_started(call_id)  # B
```

`_run_tool` — new signature and delegation branch (replace the method):

```python
    async def _run_tool(
        self, tc: ToolCall, turn: int, gate: bool = True, approvals: dict[str, bool] | None = None
    ) -> ToolResult | None:
        """Gate, execute, and filter one tool call inside its span. None when parked for approval."""
        with self._tracer.span(f"tool.{tc.tool_name}", kind=SpanKind.TOOL, tool=tc.tool_name) as tool_span:
            t0 = time.monotonic()

            def failed(error: str) -> ToolResult:
                return ToolResult(
                    call_id=tc.call_id,
                    tool_name=tc.tool_name,
                    output=None,
                    error=error,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                )

            try:
                tool = self._registry.get(tc.tool_name)
            except Exception as exc:
                tool_result = failed(str(exc))
            else:
                denial = await self._gate_tool(tc, turn) if gate else None
                if tc.call_id in self._suspended:
                    return None  # parked until Agent.resume() answers the approval
                if denial is not None:
                    tool_result = failed(f"Tool call denied: {denial}")
                else:
                    await self._mark_started(tc.call_id)
                    if isinstance(tool, AgentTool):
                        delegated = await self._delegate(tool, tc, turn, approvals or {})
                        if delegated is None:
                            return None  # the child run suspended; its approvals are parked under this call
                        tool_result = delegated
                    else:
                        try:
                            tool_result = await tool(call_id=tc.call_id, **tc.arguments)
                        except Exception as exc:
                            tool_result = failed(str(exc))
                    tool_result = await self._filter_output(tc, turn, tool_result)

            tool_span.set_attribute("duration_ms", tool_result.duration_ms)
            tool_span.set_attribute("success", tool_result.error is None)

        self._tracer.record_tool_call(tc.tool_name, tool_result.duration_ms, tool_result.error is None)
        return tool_result
```

New methods after `_run_tool`:

```python
    async def _delegate(
        self, tool: AgentTool, tc: ToolCall, turn: int, approvals: dict[str, bool]
    ) -> ToolResult | None:
        """Run a delegated child run. None when it suspended — its approvals are parked under this call."""
        remaining = (
            None if self._max_run_cost_usd is None else max(0.0, self._max_run_cost_usd - self._run_cost_usd)
        )
        ctx = DelegationContext(
            parent_run_id=self._run_id,
            call_id=tc.call_id,
            depth=self._delegation_depth + 1,
            max_depth=self._max_delegation_depth,
            context=self._context,
            hooks=self._hooks,
            approver=self._approver,
            approval_timeout_s=self._approval_timeout_s,
            run_store=self._checkpointer.store if self._checkpointer is not None else None,
            remaining_cost_usd=remaining,
            budget_guard=self._budget_guard,
            reporter=self._reporter,
            approvals=approvals,
        )
        delegation = await tool.delegate(str(tc.arguments.get("task", "")), ctx)
        self._add_delegated_spend(tc.call_id, delegation)
        if delegation.status == "suspended":
            self._suspended[tc.call_id] = delegation.approvals
            return None
        assert delegation.result is not None
        return delegation.result

    def _add_delegated_spend(self, call_id: str, delegation: Delegation) -> None:
        """Add the child's spend not yet counted in this run; remember the child's root hash."""
        if delegation.run_id is None or self._pending is None:
            return
        pending = self._pending
        self._run_cost_usd += delegation.cost_usd - pending.delegated_cost_usd.get(call_id, 0.0)
        pending.delegated_cost_usd[call_id] = delegation.cost_usd
        pending.delegated_tokens[call_id] = delegation.tokens
        if delegation.root_hash is not None:
            pending.delegated_root_hash[call_id] = delegation.root_hash
```

`_totals` — replace:

```python
    def _totals(self) -> tuple[float, int]:
        """Cost and tokens of this run's turns and the delegated runs they started."""
        cost = sum(t.cost.cost_usd + sum(r.cost_usd for r in t.tool_results) for t in self._turns)
        tokens = sum(t.cost.total_tokens + sum(r.tokens for r in t.tool_results) for t in self._turns)
        if self._pending is not None:  # delegations of the unresolved turn
            cost += sum(self._pending.delegated_cost_usd.values())
            tokens += sum(self._pending.delegated_tokens.values())
        return cost, tokens
```

`_record_tool_results` — the audit append becomes:

```python
            if self._audit:
                payload: dict[str, Any] = {
                    "call_id": tc.call_id,
                    "success": tool_result.error is None,
                    "error": tool_result.error,
                    "duration_ms": tool_result.duration_ms,
                }
                if tc.call_id in pending.delegated_cost_usd:
                    payload["delegated_run_id"] = child_run_id(self._run_id, tc.call_id)
                    payload["delegated_cost_usd"] = pending.delegated_cost_usd[tc.call_id]
                    if tc.call_id in pending.delegated_root_hash:
                        payload["delegated_root_hash"] = pending.delegated_root_hash[tc.call_id]
                self._audit.append("tool_call", actor=tc.tool_name, payload=payload)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_agent_tool.py -v`
Expected: PASS (all Task 2 and Task 3 tests). Then the gates: `python3 -m pytest -q` (including `test_durable_runs.py` and `test_hooks.py`, which cover the `_suspended` and sort changes), `ruff check agent_kit tests`, `python3 -m mypy agent_kit`.

- [ ] **Step 5: Commit**

```bash
git add agent_kit/agent/loop.py tests/test_agent_tool.py
git commit -m "feat: delegation in the agent loop — stacked policy, spend roll-up, audit links, suspension bubbling"
```

---

### Task 4: Resuming delegations — approval routing and crash recovery

**Files:**
- Modify: `agent_kit/agent/loop.py` (`_resolve_tools`, new `_resumes_delegation`)
- Test: `tests/test_agent_tool.py` (append)

**Interfaces:**
- Consumes (Task 3): `_run_tool(tc, turn, gate, approvals)`, `_suspended` lists, `ticket_run(db)`, `lead(...)`, `refunds_agent(...)`, `SimulatedCrash`.
- Produces: `parent.resume(run_id, approvals={"<call>/<child call>": bool})` resumes the child; started `AgentTool` calls with a run store resume or replay their child instead of `tool_interrupted`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_agent_tool.py`)

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_agent_tool.py -v -k "another_process or partial_child or denied_child or two_level or crash"`
Expected: FAIL — resume leaves prefixed approvals unmatched (the run stays suspended), and a started delegation is reported `Tool call interrupted before completion; not retried`.

- [ ] **Step 3: Implement** (`agent_kit/agent/loop.py`)

In `_resolve_tools`, replace the body of `resolve(tc)` up to (not including) the `if result is None:` block:

```python
        async def resolve(tc: ToolCall) -> None:
            if tc.call_id in pending.results:
                return
            approval = next((a for a in pending.approvals if a.call_id == tc.call_id), None)
            prefix = f"{tc.call_id}/"
            delegated = [a for a in pending.approvals if a.call_id.startswith(prefix)]
            result: ToolResult | None
            if approval is not None:
                if tc.call_id not in answers:
                    return
                pending.approvals.remove(approval)
                if answers[tc.call_id]:
                    self._audit_event("approval_granted", tc.tool_name, {"call_id": tc.call_id, "via": "resume"})
                    result = await self._run_tool(tc, turn_number, gate=False)
                else:
                    self._audit_event(
                        "approval_denied",
                        tc.tool_name,
                        {"call_id": tc.call_id, "timed_out": False, "error": None, "via": "resume"},
                    )
                    reason = self._deny_tool(self._tool_context(tc, turn_number), "before_tool", "approval denied")
                    result = ToolResult(
                        call_id=tc.call_id, tool_name=tc.tool_name, output=None, error=f"Tool call denied: {reason}"
                    )
            elif delegated:
                # Approvals parked by a suspended child run: route this call's answers down to it
                if not any(a.call_id in answers for a in delegated):
                    return
                for a in delegated:
                    pending.approvals.remove(a)
                child_answers = {k[len(prefix):]: v for k, v in answers.items() if k.startswith(prefix)}
                result = await self._run_tool(tc, turn_number, gate=False, approvals=child_answers)
            elif tc.call_id in pending.started and self._resumes_delegation(tc.tool_name):
                # The child run checkpoints its own tools: resume or replay it rather than report an interruption
                result = await self._run_tool(tc, turn_number, gate=False)
            elif tc.call_id in pending.started and not self._is_idempotent(tc.tool_name):
                # Started before a crash with no recorded result: never run a side effect twice
                self._audit_event("tool_interrupted", tc.tool_name, {"call_id": tc.call_id})
                result = ToolResult(
                    call_id=tc.call_id,
                    tool_name=tc.tool_name,
                    output=None,
                    error="Tool call interrupted before completion; not retried",
                )
            else:
                result = await self._run_tool(tc, turn_number)
```

New method after `_is_idempotent`:

```python
    def _resumes_delegation(self, tool_name: str) -> bool:
        """A started agent-tool call resumes its checkpointed child run instead of counting as interrupted."""
        if self._checkpointer is None:
            return False
        try:
            return isinstance(self._registry.get(tool_name), AgentTool)
        except Exception:
            return False
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_agent_tool.py -v`
Expected: PASS (all). Then the gates: `python3 -m pytest -q`, `ruff check agent_kit tests`, `python3 -m mypy agent_kit`.

- [ ] **Step 5: Commit**

```bash
git add agent_kit/agent/loop.py tests/test_agent_tool.py
git commit -m "feat: resume delegations — route child approvals down, recover children from their checkpoints"
```

---

### Task 5: Live verification (scratchpad, not committed)

Local Ollama (`llama3.2`) is the live provider; no Anthropic/OpenAI keys are in the environment.

- [ ] `e2e-delegation/suspend.py` (process A): `lead = Agent(OllamaProvider("llama3.2"), tools=[research, refunds], config=AgentConfig(run_store=SQLiteRunStore("runs.db"), approver=SUSPEND, max_run_cost_usd=2.00))` where `research` is an agent tool with a `lookup_order` tool and `refunds` is an agent tool whose `issue_refund` is behind `require_approval`; prompt the lead to look up order A-1001 and refund it; `run_id="live-1"`; print status and pending approvals (call id, tool, run id); exit.
- [ ] `e2e-delegation/approve.py` (process B): the same agents; `await lead.resume("live-1", approvals={<printed id>: True})`; print status, output, `total_cost_usd`, refund executions (appended to a file — expect exactly one), and `lead.audit.verify()`.
- [ ] `e2e-delegation/policy.py`: lead with `deny_tools("issue_refund", reason="refunds frozen")`; confirm the refunds child's model sees `Tool call denied: refunds frozen` and no refund executes.
- [ ] `e2e-delegation/kill.py`: the refunds child gets a non-idempotent `slow_refund` that writes a marker then sleeps 30s, with `approver=None` on the lead; run the lead in a subprocess, `SIGKILL` once the marker exists, resume the lead in-process; confirm the child resumed from its checkpoint (child run shows `tool_interrupted` for `slow_refund`, lead does not) and the marker count is 1.

Record anything the live runs contradict as a spec/plan issue before Task 6.

---

### Task 6: Docs

**Files:**
- Create: `examples/delegation.py`
- Modify: `README.md` ("Agents as tools" section after "Durable runs"), `CHANGELOG.md` (`[Unreleased]` → Added), `examples/README.md` (row), `specs/06-harness-roadmap.md` (2.6 ticked; table row), `specs/16-agent-as-tool.md` (status), `PROJECT_INDEX.md` + `PROJECT_INDEX.json`

- [ ] **Step 1: `examples/delegation.py`**

```python
"""Agents as tools: a support lead delegates to a researcher and a refunds agent whose refunds need approval.

    ANTHROPIC_API_KEY=... python examples/delegation.py start
    ANTHROPIC_API_KEY=... python examples/delegation.py approve <call_id>
"""

import asyncio
import sys

from agent_kit import SUSPEND, Agent, AgentConfig, tool
from agent_kit.durable import SQLiteRunStore
from agent_kit.hooks import Hooks, deny_tools, require_approval
from agent_kit.providers import AnthropicProvider

RUN_ID = "ticket-9914"


@tool(description="Look up an order by id", idempotent=True)
async def lookup_order(order_id: str) -> dict:
    return {"order_id": order_id, "status": "delivered", "total_usd": 84.00}


@tool(description="Refund an order in full")
async def issue_refund(order_id: str) -> dict:
    return {"order_id": order_id, "refunded": True}


@tool(description="Close a customer account")
async def close_account(customer_id: str) -> dict:
    return {"customer_id": customer_id, "closed": True}


def build_lead() -> Agent:
    research = Agent(
        AnthropicProvider(),
        tools=[lookup_order],
        config=AgentConfig(system_prompt="Answer questions about orders using lookup_order. Be brief."),
    ).as_tool("research", "Look up facts about orders.")
    refunds = Agent(
        AnthropicProvider(),
        tools=[lookup_order, issue_refund, close_account],
        config=AgentConfig(
            system_prompt="Handle refund requests.",
            hooks=Hooks(before_tool=[require_approval("issue_refund", reason="refunds need a human")]),
        ),
    ).as_tool("refunds", "Handle a refund request end to end.")
    return Agent(
        AnthropicProvider(),
        tools=[research, refunds],
        config=AgentConfig(
            system_prompt="You lead customer support. Delegate research and refunds.",
            run_store=SQLiteRunStore("runs.db"),
            approver=SUSPEND,  # approvals from any child suspend the whole ticket
            hooks=Hooks(before_tool=[deny_tools("close_account", reason="account closure is manual")]),
            max_run_cost_usd=1.00,  # covers the lead and every delegated run
        ),
    )


async def main() -> None:
    lead = build_lead()
    if sys.argv[1:2] == ["start"]:
        result = await lead.run("Ticket 9914: customer says order A-1001 arrived broken and wants a refund.", run_id=RUN_ID)
        for pending in result.pending_approvals:
            print(f"waiting for approval: {pending.tool_name}({pending.arguments}) — call id {pending.call_id}")
    else:
        result = await lead.resume(RUN_ID, approvals={sys.argv[2]: True})
        print(result.status, "-", result.output, f"(${result.total_cost_usd:.4f} across all runs)")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: README section** — "Agents as tools" after "Durable runs": the `as_tool` call; each call is a fresh child run; what the child keeps vs inherits (table from the spec's "Building a child run"); name-based parent policies (`deny_tools`, `allow_only`) see child tool names, so `allow_only` on a parent must list the child's tools too; approvals bubble with `call/child_call` ids; spend rolls into the parent cap and `total_cost_usd`; the parent's `tool_call` audit event carries `delegated_run_id` / `delegated_root_hash`; `DAGOrchestrator` for static graphs. Link `examples/delegation.py` and `specs/16-agent-as-tool.md`.

- [ ] **Step 3: CHANGELOG** — `[Unreleased]` → Added, first bullet:

```markdown
- **Agents as tools.** `agent.as_tool(name, description, output_type=None)` lets another agent delegate to it. Every call is a fresh child run (own memory and audit chain) that keeps the child's provider, tools, and hooks and inherits the calling run's hooks (run after the child's, so delegation can't bypass a `deny_tools`), approver, run store, and remaining `max_run_cost_usd`. A child suspended on an approval suspends the parent; its approvals appear in `pending_approvals` as `"<call>/<child call>"` and `parent.resume(run_id, approvals=...)` routes them down. Child spend counts toward the parent cap and `total_cost_usd`; the parent's `tool_call` audit event records `delegated_run_id` and `delegated_root_hash`; child runs report to Cloud with `parent_run_id` on `run_start`. Crashed delegations resume the child from its checkpoint. `AgentConfig(max_delegation_depth=5)`. `ToolResult` gains `cost_usd` / `tokens`; `PendingApproval` gains `run_id`. With `cloud` set, a caller-supplied `run_id` longer than 36 characters raises `ValueError`. Example `examples/delegation.py`. See `specs/16-agent-as-tool.md`.
```

- [ ] **Step 4: Roadmap, spec, examples index, project index**
  - `specs/06-harness-roadmap.md`: `- [x] **2.6 Agent-as-tool**`; table row `| Sub-agents / handoffs | Delegation ✅ (2.6); no handoffs | ✅ | ✅ |`.
  - `specs/16-agent-as-tool.md`: `Status: **implemented**`.
  - `examples/README.md`: row `| [`delegation.py`](delegation.py) | <line count> | Agents as tools — a support lead delegates to a researcher and a refunds agent; the refund's approval suspends the whole ticket (`start`) and `approve <call_id>` resumes it. | API key |`.
  - `PROJECT_INDEX.md` / `PROJECT_INDEX.json`: `agent/delegation.py` in the tree, feature-map row (spec 16, `examples/delegation.py`), `AgentConfig.max_delegation_depth`, `as_tool`, `tests/test_agent_tool.py`, updated test counts.

- [ ] **Step 5: Gates and commit**

Run: `python3 -m pytest -q`, `cd server && python3 -m pytest -q`, `ruff check agent_kit tests`, `python3 -m mypy agent_kit`, `python3 -m py_compile examples/*.py`.

```bash
git add examples/delegation.py examples/README.md README.md CHANGELOG.md specs/06-harness-roadmap.md specs/16-agent-as-tool.md PROJECT_INDEX.md PROJECT_INDEX.json
git commit -m "docs: agents as tools guide and example"
```
