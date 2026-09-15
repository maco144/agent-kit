# Durable Runs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Runs checkpoint at turn boundaries into a `RunStore`, can suspend on approvals (`approver=SUSPEND`) and be resumed from another process with `agent.resume(run_id, approvals=...)`, recover after crashes without re-running non-idempotent tools, and resist concurrent resumes through compare-and-swap versions.

**Architecture:** A new `agent_kit/durable/` package holds the checkpoint models, the async `RunStore` protocol with `SQLiteRunStore`, and a `Checkpointer` that serialises one loop's writes and tracks the CAS version. `AgentLoop._execute` gains a restore path and a unified "resolve the pending turn" step (`_resolve_tools`) shared by fresh tool turns and resumes; approvals answered with `SUSPEND` park calls in `PendingTurn.approvals`. `Agent.resume` loads, validates, and restores memory and the audit chain, then drives the loop.

**Tech Stack:** Python 3.11+, Pydantic v2, sqlite3 via `asyncio.to_thread`, pytest-asyncio, mypy strict, ruff.

**Spec:** `specs/15-durable-runs.md`

## Global Constraints

- `agent_kit/types.py` imports nothing from `agent_kit`; `agent_kit/durable/` imports only `agent_kit.types` and `agent_kit.exceptions`.
- Store CAS: `save(checkpoint, expected_version)` writes `version = expected_version + 1`; `0` means must-not-exist; mismatch → `RunConflictError`.
- Checkpoint JSON is produced in the event-loop thread (`model_dump_json()` before `to_thread`) so concurrent tool coroutines never mutate an object mid-serialisation.
- Without `run_store` behaviour is unchanged except `AgentResult.run_id`, `status`, and totals summed from the run's turns.
- Messages: `"run context must be JSON-serialisable when run_store is set"`, `"run '<id>' already exists; use agent.resume()"`, `"Tool call interrupted before completion; not retried"`, `"Tool call denied: approval denied"`.
- Tests simulate crashes with a `BaseException` subclass (never `KeyboardInterrupt`, which aborts pytest).
- Gates per task: `python3 -m pytest -q`, `$V/python -m pytest -q` (V = scratchpad `venv-harness/bin`), `ruff check agent_kit tests`, `$V/python -m mypy agent_kit`.

---

### Task 1: Result fields, exceptions, `SUSPEND`, `AuditChain.restore`, run totals

**Files:** Modify `agent_kit/types.py`, `agent_kit/exceptions.py`, `agent_kit/hooks.py`, `agent_kit/audit/chain.py`, `agent_kit/agent/loop.py` (totals only), `agent_kit/__init__.py`. Test: `tests/test_audit.py`, `tests/test_agent.py`.

**Produces:** `PendingApproval`; `AgentResult.run_id/status/pending_approvals`; `RunNotFoundError(run_id)`, `RunConflictError(run_id, expected_version, actual_version)`, `CheckpointError(run_id, reason)`; `hooks.SUSPEND` (instance of `hooks.Suspend`); `AuditChain.restore(events) -> AuditChain`.

- [ ] **Step 1: Failing tests**

`tests/test_audit.py`:

```python
def test_restore_continues_the_chain():
    chain = AuditChain()
    chain.append("a", actor="x", payload={"n": 1})
    chain.append("b", actor="x")
    restored = AuditChain.restore(chain.events())
    assert restored.root_hash() == chain.root_hash()
    restored.append("c", actor="x")
    assert restored.verify()
    assert AuditChain.restore([]).root_hash() == AuditChain().root_hash()


def test_restore_rejects_a_tampered_chain():
    from agent_kit.exceptions import AuditVerificationError

    chain = AuditChain()
    chain.append("a", actor="x")
    chain.append("b", actor="x")
    events = chain.events()
    events[0] = events[0].model_copy(update={"event_type": "forged"})
    with pytest.raises(AuditVerificationError):
        AuditChain.restore(events)
```

`tests/test_agent.py`:

```python
async def test_result_totals_cover_only_this_run(mock_provider):
    agent = Agent(mock_provider)
    first = await agent.run("one")
    second = await agent.run("two")
    assert second.total_cost_usd == pytest.approx(first.total_cost_usd)
    assert second.total_tokens == first.total_tokens == 15
    assert second.run_id and second.run_id != first.run_id
    assert (second.status, second.pending_approvals) == ("completed", [])
```

- [ ] **Step 2: Run** → FAIL (`AttributeError: restore`, totals doubled on second run, no `run_id`).

- [ ] **Step 3: Implement**

`types.py` (before `AgentResult`):

```python
class PendingApproval(BaseModel):
    """A tool call waiting for a human decision in a suspended run."""

    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    reason: str | None = None
    turn: int
```

`AgentResult` gains:

```python
    run_id: str | None = None
    status: Literal["completed", "suspended"] = "completed"
    pending_approvals: list[PendingApproval] = Field(default_factory=list)
```

`exceptions.py`:

```python
class RunNotFoundError(AgentKitError):
    """No checkpoint exists for the run id."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"No checkpoint for run '{run_id}'.")
        self.run_id = run_id


class RunConflictError(AgentKitError):
    """A run checkpoint changed underneath this writer (another worker owns the run)."""

    def __init__(self, run_id: str, expected_version: int, actual_version: int | None) -> None:
        super().__init__(
            f"Run '{run_id}' changed concurrently (expected version {expected_version}, found {actual_version})."
        )
        self.run_id = run_id
        self.expected_version = expected_version
        self.actual_version = actual_version


class CheckpointError(AgentKitError):
    """A checkpoint exists but cannot be resumed."""

    def __init__(self, run_id: str, reason: str) -> None:
        super().__init__(f"Checkpoint for run '{run_id}' cannot be resumed: {reason}")
        self.run_id = run_id
        self.reason = reason
```

`hooks.py`:

```python
class Suspend:
    """Approver sentinel: suspend the run until the approval is answered via Agent.resume()."""

    def __repr__(self) -> str:
        return "SUSPEND"


SUSPEND: Final = Suspend()
```

`chain.py`:

```python
    @classmethod
    def restore(cls, events: list[AuditEventRecord]) -> AuditChain:
        """Rebuild a chain from recorded events (e.g. a run checkpoint) and verify it."""
        chain = cls()
        chain._events = list(events)
        chain._current_root = events[-1].leaf_hash if events else _GENESIS_ROOT
        chain.verify()
        return chain
```

`loop.py`: totals from the run's turns — `agent_complete` payload, root span attribute, and `AgentResult` use

```python
    def _totals(self) -> tuple[float, int]:
        return sum(t.cost.cost_usd for t in self._turns), sum(t.cost.total_tokens for t in self._turns)
```

and `AgentResult(..., run_id=run_id)`. `agent_kit/__init__.py` exports `SUSPEND`.

- [ ] **Step 4: Gates.** — [ ] **Step 5: Commit** `feat: run ids, suspended status fields, durable-run errors, audit chain restore`.

---

### Task 2: `agent_kit/durable` — checkpoint models, `RunStore`, `SQLiteRunStore`, `Checkpointer`

**Files:** Create `agent_kit/durable/__init__.py`, `agent_kit/durable/models.py`, `agent_kit/durable/store.py`, `agent_kit/durable/checkpointer.py`. Test: `tests/test_run_store.py`.

**Produces:** `RunStatus`, `CHECKPOINT_SCHEMA_VERSION = 1`, `PendingTurn`, `RunCheckpoint`, `RunSummary`, `RunStore` (Protocol), `SQLiteRunStore(path=":memory:")` with `close()`, `Checkpointer(store)` with `.store`, `.current`, `create(cp)`, `save(cp)`, `mark_started(call_id)`, `fail(error)`.

- [ ] **Step 1: Failing tests** — `tests/test_run_store.py`:

```python
"""SQLiteRunStore and Checkpointer."""

from __future__ import annotations

from datetime import datetime

import pytest

from agent_kit.audit.chain import AuditChain
from agent_kit.durable import Checkpointer, PendingTurn, RunCheckpoint, SQLiteRunStore
from agent_kit.exceptions import RunConflictError
from agent_kit.types import Message, PendingApproval, ToolCall, ToolResult, Turn


def checkpoint(run_id: str = "r1", **update: object) -> RunCheckpoint:
    chain = AuditChain()
    chain.append("agent_start", actor=run_id, payload={"p": 1})
    tc = ToolCall(tool_name="refund", arguments={"order_id": "42"}, call_id="c1")
    now = datetime.utcnow()
    cp = RunCheckpoint(
        run_id=run_id, version=0, status="running", created_at=now, updated_at=now,
        prompt="refund 42", context={"tenant": "acme"}, output_type_name=None,
        messages=[
            Message(role="user", content="refund 42"),
            Message(role="assistant", content="", tool_calls=[tc],
                    native_content=[{"type": "thinking", "thinking": "", "signature": "s"}], native_provider="anthropic"),
        ],
        turns=[], turn_count=1, run_cost_usd=0.01, invalid_answers=0, native_output=False,
        last_prompt_tokens=10, last_prompt_chars=40, audit_events=chain.events(),
        pending=PendingTurn(
            turn=Turn(tool_calls=[tc]),
            results={"c0": ToolResult(call_id="c0", tool_name="lookup", output={"ok": True})},
            approvals=[PendingApproval(call_id="c1", tool_name="refund", arguments={"order_id": "42"}, turn=1)],
        ),
    )
    return cp.model_copy(update=update)


@pytest.fixture(params=["memory", "file"])
def store(request, tmp_path):
    s = SQLiteRunStore(":memory:" if request.param == "memory" else tmp_path / "runs.db")
    yield s
    s.close()


async def test_insert_load_round_trip(store):
    saved = await store.save(checkpoint(), 0)
    assert saved.version == 1
    loaded = await store.load("r1")
    assert loaded == saved
    assert loaded.messages[1].native_content == [{"type": "thinking", "thinking": "", "signature": "s"}]
    assert AuditChain.restore(loaded.audit_events).verify()
    assert await store.load("missing") is None


async def test_cas_conflicts(store):
    await store.save(checkpoint(), 0)
    with pytest.raises(RunConflictError) as exc:
        await store.save(checkpoint(), 0)
    assert (exc.value.expected_version, exc.value.actual_version) == (0, 1)
    v2 = await store.save(checkpoint(status="suspended"), 1)
    assert v2.version == 2
    with pytest.raises(RunConflictError):
        await store.save(checkpoint(), 1)
    with pytest.raises(RunConflictError) as missing:
        await store.save(checkpoint("nope"), 3)
    assert missing.value.actual_version is None


async def test_mark_tool_started(store):
    await store.save(checkpoint(), 0)
    assert await store.mark_tool_started("r1", "c1", 1) == 2
    assert (await store.load("r1")).pending.started == ["c1"]
    with pytest.raises(RunConflictError):
        await store.mark_tool_started("r1", "c2", 1)


async def test_list_and_delete(store):
    await store.save(checkpoint("a"), 0)
    await store.save(checkpoint("b", status="suspended"), 0)
    await store.save(checkpoint("c", status="completed", pending=None), 0)
    assert {s.run_id for s in await store.list()} == {"a", "b", "c"}
    suspended = await store.list(status="suspended")
    assert [(s.run_id, s.pending_approvals) for s in suspended] == [("b", 1)]
    await store.delete("b")
    assert await store.load("b") is None


async def test_checkpointer_tracks_versions_and_fail(store):
    cp = Checkpointer(store)
    await cp.create(checkpoint())
    await cp.mark_started("c1")
    await cp.save(checkpoint(turn_count=2))
    assert cp.current is not None and cp.current.version == 3
    await cp.fail("RuntimeError: boom")
    failed = await store.load("r1")
    assert (failed.status, failed.error, failed.version, failed.turn_count) == ("failed", "RuntimeError: boom", 4, 2)
```

- [ ] **Step 2: Run** → `ModuleNotFoundError: agent_kit.durable`.

- [ ] **Step 3: Implement**

`agent_kit/durable/models.py`:

```python
"""Checkpoint models for durable runs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from agent_kit.types import AuditEventRecord, Message, PendingApproval, ToolResult, Turn

RunStatus = Literal["running", "suspended", "completed", "failed"]
CHECKPOINT_SCHEMA_VERSION = 1


class PendingTurn(BaseModel):
    """A model turn whose tool calls are not all resolved yet."""

    turn: Turn
    results: dict[str, ToolResult] = Field(default_factory=dict)  # call_id → final (post-hook) result
    started: list[str] = Field(default_factory=list)  # call_ids whose tool execution began
    approvals: list[PendingApproval] = Field(default_factory=list)  # calls awaiting a decision


class RunCheckpoint(BaseModel):
    """Everything needed to continue a run in another process."""

    schema_version: int = CHECKPOINT_SCHEMA_VERSION
    run_id: str
    version: int
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    prompt: str
    context: dict[str, Any]
    output_type_name: str | None
    messages: list[Message]
    turns: list[Turn]
    turn_count: int
    run_cost_usd: float
    invalid_answers: int
    native_output: bool
    last_prompt_tokens: int
    last_prompt_chars: int
    audit_events: list[AuditEventRecord]
    pending: PendingTurn | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


class RunSummary(BaseModel):
    run_id: str
    status: RunStatus
    updated_at: datetime
    pending_approvals: int
```

`agent_kit/durable/store.py`:

```python
"""RunStore protocol and the SQLite implementation."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from agent_kit.durable.models import RunCheckpoint, RunStatus, RunSummary
from agent_kit.exceptions import CheckpointError, RunConflictError


@runtime_checkable
class RunStore(Protocol):
    """Durable storage for run checkpoints. Every write is compare-and-swap on ``version``."""

    async def save(self, checkpoint: RunCheckpoint, expected_version: int) -> RunCheckpoint: ...
    async def load(self, run_id: str) -> RunCheckpoint | None: ...
    async def mark_tool_started(self, run_id: str, call_id: str, expected_version: int) -> int: ...
    async def list(self, status: RunStatus | None = None, limit: int = 100) -> list[RunSummary]: ...
    async def delete(self, run_id: str) -> None: ...


class SQLiteRunStore:
    """
    Run checkpoints in a SQLite file, shared by every process that opens the same path.

    Usage::

        store = SQLiteRunStore("~/.agent-kit/runs.db")
        agent = Agent(provider, config=AgentConfig(run_store=store, approver=SUSPEND))
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = ":memory:" if str(path) == ":memory:" else str(Path(path).expanduser())
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False, timeout=30)
        with self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, version INTEGER NOT NULL, "
                "status TEXT NOT NULL, updated_at TEXT NOT NULL, pending_approvals INTEGER NOT NULL, "
                "checkpoint TEXT NOT NULL)"
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS runs_status ON runs (status)")

    async def save(self, checkpoint: RunCheckpoint, expected_version: int) -> RunCheckpoint:
        stored = checkpoint.model_copy(update={"version": expected_version + 1, "updated_at": datetime.utcnow()})
        data = stored.model_dump_json()  # serialise in the caller's thread
        pending = len(stored.pending.approvals) if stored.pending else 0
        await asyncio.to_thread(self._write, stored, expected_version, data, pending)
        return stored

    def _write(self, cp: RunCheckpoint, expected: int, data: str, pending: int) -> None:
        row = (cp.version, cp.status, cp.updated_at.isoformat(), pending, data)
        with self._lock, self._conn:
            if expected == 0:
                try:
                    self._conn.execute(
                        "INSERT INTO runs (version, status, updated_at, pending_approvals, checkpoint, run_id) "
                        "VALUES (?,?,?,?,?,?)",
                        (*row, cp.run_id),
                    )
                except sqlite3.IntegrityError:
                    raise RunConflictError(cp.run_id, 0, self._version(cp.run_id)) from None
                return
            cursor = self._conn.execute(
                "UPDATE runs SET version=?, status=?, updated_at=?, pending_approvals=?, checkpoint=? "
                "WHERE run_id=? AND version=?",
                (*row, cp.run_id, expected),
            )
            if cursor.rowcount != 1:
                raise RunConflictError(cp.run_id, expected, self._version(cp.run_id))

    def _version(self, run_id: str) -> int | None:
        found = self._conn.execute("SELECT version FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return int(found[0]) if found else None

    async def load(self, run_id: str) -> RunCheckpoint | None:
        return await asyncio.to_thread(self._load, run_id)

    def _load(self, run_id: str) -> RunCheckpoint | None:
        with self._lock:
            found = self._conn.execute("SELECT checkpoint FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return RunCheckpoint.model_validate_json(found[0]) if found else None

    async def mark_tool_started(self, run_id: str, call_id: str, expected_version: int) -> int:
        return await asyncio.to_thread(self._mark, run_id, call_id, expected_version)

    def _mark(self, run_id: str, call_id: str, expected: int) -> int:
        with self._lock, self._conn:
            found = self._conn.execute("SELECT version, checkpoint FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if found is None or int(found[0]) != expected:
                raise RunConflictError(run_id, expected, int(found[0]) if found else None)
            cp = RunCheckpoint.model_validate_json(found[1])
            if cp.pending is None:
                raise CheckpointError(run_id, "no pending turn to mark a tool call started")
            cp.pending.started.append(call_id)
            cp.version = expected + 1
            self._conn.execute(
                "UPDATE runs SET version=?, checkpoint=? WHERE run_id=?",
                (cp.version, cp.model_dump_json(), run_id),
            )
            return cp.version

    async def list(self, status: RunStatus | None = None, limit: int = 100) -> list[RunSummary]:
        return await asyncio.to_thread(self._list, status, limit)

    def _list(self, status: RunStatus | None, limit: int) -> list[RunSummary]:
        query = "SELECT run_id, status, updated_at, pending_approvals FROM runs"
        params: tuple[object, ...] = ()
        if status is not None:
            query += " WHERE status=?"
            params = (status,)
        query += " ORDER BY updated_at DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(query, (*params, limit)).fetchall()
        return [
            RunSummary(run_id=r[0], status=r[1], updated_at=datetime.fromisoformat(r[2]), pending_approvals=r[3])
            for r in rows
        ]

    async def delete(self, run_id: str) -> None:
        await asyncio.to_thread(self._delete, run_id)

    def _delete(self, run_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM runs WHERE run_id=?", (run_id,))

    def close(self) -> None:
        self._conn.close()
```

`agent_kit/durable/checkpointer.py`:

```python
"""Serialised checkpoint writes for one agent loop."""

from __future__ import annotations

import asyncio
import logging

from agent_kit.durable.models import RunCheckpoint
from agent_kit.durable.store import RunStore

logger = logging.getLogger(__name__)


class Checkpointer:
    """Owns one run's CAS version; parallel tool coroutines write through its lock."""

    def __init__(self, store: RunStore) -> None:
        self.store = store
        self.current: RunCheckpoint | None = None
        self._lock = asyncio.Lock()

    async def create(self, checkpoint: RunCheckpoint) -> None:
        async with self._lock:
            self.current = await self.store.save(checkpoint, 0)

    async def save(self, checkpoint: RunCheckpoint) -> None:
        async with self._lock:
            expected = self.current.version if self.current else 0
            self.current = await self.store.save(checkpoint, expected)

    async def mark_started(self, call_id: str) -> None:
        async with self._lock:
            assert self.current is not None and self.current.pending is not None
            version = await self.store.mark_tool_started(self.current.run_id, call_id, self.current.version)
            pending = self.current.pending.model_copy(update={"started": [*self.current.pending.started, call_id]})
            self.current = self.current.model_copy(update={"version": version, "pending": pending})

    async def fail(self, error: str) -> None:
        """Mark the last saved checkpoint failed, keeping its state so resume retries from there."""
        async with self._lock:
            if self.current is None:
                return
            try:
                self.current = await self.store.save(
                    self.current.model_copy(update={"status": "failed", "error": error}), self.current.version
                )
            except Exception:  # the run is already failing; don't mask its error
                logger.warning("could not record failure for run %s", self.current.run_id, exc_info=True)
```

`agent_kit/durable/__init__.py`:

```python
"""Durable runs: checkpoints, run stores, suspend and resume."""

from agent_kit.durable.checkpointer import Checkpointer
from agent_kit.durable.models import CHECKPOINT_SCHEMA_VERSION, PendingTurn, RunCheckpoint, RunStatus, RunSummary
from agent_kit.durable.store import RunStore, SQLiteRunStore

__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "Checkpointer",
    "PendingTurn",
    "RunCheckpoint",
    "RunStatus",
    "RunStore",
    "RunSummary",
    "SQLiteRunStore",
]
```

- [ ] **Step 4: Gates.** — [ ] **Step 5: Commit** `feat: run checkpoints and SQLiteRunStore with compare-and-swap writes`.

---

### Task 3: Loop and Agent — checkpointing, suspension, resume

**Files:** Modify `agent_kit/agent/loop.py`, `agent_kit/agent/agent.py`. Test: `tests/test_durable_runs.py`.

**Consumes:** Tasks 1–2. **Produces:** `AgentConfig(run_store=...)`, `approver` accepting `SUSPEND`; `Agent.run/stream(..., run_id=None)`; `Agent.resume/resume_stream(run_id, *, approvals=None, output_type=None)`; `AgentLoop(..., run_store=None)`, `AgentLoop.resume(checkpoint, approvals, output_type)`, `AgentLoop.resume_stream(...)`.

- [ ] **Step 1: Failing tests** — `tests/test_durable_runs.py`:

```python
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
```

- [ ] **Step 2: Run** → FAIL (`AgentConfig` has no `run_store`).

- [ ] **Step 3: Implement — `agent_kit/agent/agent.py`**

- Imports: `from agent_kit.audit.chain import AuditChain` (already), `from agent_kit.durable import CHECKPOINT_SCHEMA_VERSION, RunCheckpoint, RunStore`, `from agent_kit.exceptions import AuditVerificationError, CheckpointError, RunNotFoundError`, `from agent_kit.hooks import SUSPEND, Suspend` (runtime import; hooks imports nothing from agent), `from agent_kit.output import OutputSpec`.
- `AgentConfig.__init__`: `approver: Approver | Suspend | None = None`, `run_store: RunStore | None = None` → `self.run_store = run_store  # checkpoints for resume and suspension`.
- `Agent.__init__`: after the budgets check: `if self._config.approver is SUSPEND and self._config.run_store is None: raise ValueError("approver=SUSPEND requires AgentConfig(run_store=...)")`.
- `run` / `stream` gain `run_id: str | None = None` (keyword-only, in overloads too) and pass `run_id=run_id` to the loop.
- `_make_loop` passes `run_store=self._config.run_store`.
- New methods:

```python
    @overload
    async def resume(self, run_id: str, *, approvals: dict[str, bool] | None = None, output_type: type[T]) -> AgentResult[T]: ...

    @overload
    async def resume(self, run_id: str, *, approvals: dict[str, bool] | None = None, output_type: None = None) -> AgentResult[Any]: ...

    async def resume(
        self, run_id: str, *, approvals: dict[str, bool] | None = None, output_type: Any = None
    ) -> AgentResult[Any]:
        """
        Continue a checkpointed run — after a crash, a failure, or a suspension for approval.

        ``approvals`` answers pending approvals by call id. Rebuild the Agent with the same tools and hooks;
        memory and the audit chain are restored from the checkpoint.
        """
        checkpoint = await self._load_checkpoint(run_id)
        if checkpoint.status == "completed":
            self.last_result = self._stored_result(checkpoint, output_type)
            return self.last_result
        self._restore(checkpoint, output_type)
        self.last_result = await self._make_loop().resume(checkpoint, approvals or {}, output_type)
        return self.last_result

    async def resume_stream(
        self, run_id: str, *, approvals: dict[str, bool] | None = None, output_type: Any = None
    ) -> AsyncIterator[str]:
        """Streaming resume(); ``agent.last_result`` is set when the iterator is exhausted."""
        checkpoint = await self._load_checkpoint(run_id)
        if checkpoint.status == "completed":
            self.last_result = self._stored_result(checkpoint, output_type)
            return
        self._restore(checkpoint, output_type)
        loop = self._make_loop()
        async for chunk in loop.resume_stream(checkpoint, approvals or {}, output_type):
            yield chunk
        self.last_result = loop.result

    async def _load_checkpoint(self, run_id: str) -> RunCheckpoint:
        if self._config.run_store is None:
            raise ValueError("resume() requires AgentConfig(run_store=...)")
        checkpoint = await self._config.run_store.load(run_id)
        if checkpoint is None:
            raise RunNotFoundError(run_id)
        if checkpoint.schema_version > CHECKPOINT_SCHEMA_VERSION:
            raise CheckpointError(
                run_id, f"schema version {checkpoint.schema_version} is newer than {CHECKPOINT_SCHEMA_VERSION}"
            )
        return checkpoint

    @staticmethod
    def _stored_result(checkpoint: RunCheckpoint, output_type: Any) -> AgentResult[Any]:
        result: AgentResult[Any] = AgentResult.model_validate(checkpoint.result or {"output": ""})
        if output_type is not None and result.parsed is not None:
            result.parsed = OutputSpec.from_type(output_type).adapter.validate_python(result.parsed)
        return result

    def _restore(self, checkpoint: RunCheckpoint, output_type: Any) -> None:
        name = OutputSpec.from_type(output_type).name if output_type is not None else None
        if name != checkpoint.output_type_name:
            raise CheckpointError(
                checkpoint.run_id, f"output_type {name!r} does not match the run's {checkpoint.output_type_name!r}"
            )
        if self._audit is not None:
            try:
                self._audit = AuditChain.restore(checkpoint.audit_events)
            except AuditVerificationError as exc:
                raise CheckpointError(checkpoint.run_id, f"audit chain failed verification: {exc}") from exc
        self._memory.clear()
        self._memory.add_many(checkpoint.messages)
```

- [ ] **Step 4: Implement — `agent_kit/agent/loop.py`**

Imports: `from datetime import datetime`; `from agent_kit.durable import Checkpointer, PendingTurn, RunCheckpoint, RunStatus, RunStore`; `RunConflictError` from exceptions; `SUSPEND` from hooks; `PendingApproval` from types.

`__init__` gains `run_store: RunStore | None = None` → `self._checkpointer = Checkpointer(run_store) if run_store is not None else None`, plus `self._prompt = ""`, `self._output_type_name: str | None = None`, `self._pending: PendingTurn | None = None`, `self._suspended: dict[str, PendingApproval] = {}`.

Entry points:

```python
    async def run(self, prompt: str, output_type: Any = None, run_id: str | None = None, **context: Any) -> AgentResult[Any]:
        async for _ in self._execute(prompt, False, context, output_type, run_id=run_id):
            pass
        assert self.result is not None
        return self.result

    async def stream(self, prompt: str, output_type: Any = None, run_id: str | None = None, **context: Any) -> AsyncIterator[str]:
        async for chunk in self._execute(prompt, True, context, output_type, run_id=run_id):
            yield chunk

    async def resume(self, checkpoint: RunCheckpoint, approvals: dict[str, bool], output_type: Any = None) -> AgentResult[Any]:
        async for _ in self._execute(checkpoint.prompt, False, checkpoint.context, output_type, restore=checkpoint, approvals=approvals):
            pass
        assert self.result is not None
        return self.result

    async def resume_stream(self, checkpoint: RunCheckpoint, approvals: dict[str, bool], output_type: Any = None) -> AsyncIterator[str]:
        async for chunk in self._execute(checkpoint.prompt, True, checkpoint.context, output_type, restore=checkpoint, approvals=approvals):
            yield chunk
```

`_execute(self, prompt, streaming, context, output_type=None, *, run_id=None, restore=None, approvals=None)`:

1. `run_id = restore.run_id if restore else (run_id or str(uuid.uuid4()))`; set `self._run_id`, `self._context`, `self._prompt`.
2. Output spec as today, except `native = restore.native_output if restore else <today's expression>`; `invalid_answers = restore.invalid_answers if restore else 0`; `self._output_type_name = spec.name if spec else None`; `system` / `output_kwargs` derived from `native` afterwards.
3. Fresh run with a checkpointer: JSON check of `context` (TypeError message above), then `if await self._checkpointer.store.load(run_id) is not None: raise ValueError(f"run '{run_id}' already exists; use agent.resume()")`.
4. `on_run_start` only when `restore is None`.
5. Inside the root span:

```python
            turn_count = 0
            resumed_pending: PendingTurn | None = None
            if restore is None:
                # agent_start audit + seed prompt, as today
                if self._checkpointer is not None:
                    await self._checkpointer.create(self._snapshot("running", turn_count, invalid_answers, native))
            else:
                assert self._checkpointer is not None
                turn_count = restore.turn_count
                self._turns = list(restore.turns)
                self._run_cost_usd = restore.run_cost_usd
                self._last_prompt_tokens = restore.last_prompt_tokens
                self._last_prompt_chars = restore.last_prompt_chars
                resumed_pending = restore.pending
                self._checkpointer.current = restore
                await self._checkpointer.save(restore.model_copy(update={"status": "running", "error": None}))
                self._audit_event("run_resumed", run_id, {"turn": turn_count, "from_status": restore.status})

            final_output = ""
            suspended = False
            try:
                while True:
                    if resumed_pending is not None:
                        self._pending, resumed_pending = resumed_pending, None
                        turn = self._pending.turn
                        answers = approvals or {}
                    else:
                        if turn_count >= self._max_turns:
                            raise MaxTurnsExceededError(self._max_turns)
                        turn_count += 1
                        # ... unchanged: budgets, trimming, gate, model call, audits, cost, add assistant message,
                        # ... and the no-tool-calls final-answer block (break / continue for repairs)
                        self._pending = PendingTurn(turn=turn)
                        answers = {}
                        await self._checkpoint("running", turn_count, invalid_answers, native)  # A

                    waiting = await self._resolve_tools(turn_count, self._pending, answers)
                    if waiting:
                        self._audit_event(
                            "run_suspended", run_id,
                            {"turn": turn_count, "pending_call_ids": [a.call_id for a in waiting]},
                        )
                        await self._checkpoint("suspended", turn_count, invalid_answers, native)  # D
                        suspended = True
                        break
                    self._record_tool_results(self._pending)
                    self._pending = None
                    self._turns.append(turn)
                    if self._reporter:
                        await self._reporter.on_turn_complete(run_id, turn, len(self._turns) - 1)
                    await self._checkpoint("running", turn_count, invalid_answers, native)  # C
                    if self._pending_stop is not None:
                        raise self._pending_stop

            except Exception as exc:
                if self._reporter:
                    await self._reporter.on_run_error(run_id, exc, turn_count)
                if self._checkpointer is not None and not isinstance(exc, RunConflictError):
                    await self._checkpointer.fail(f"{type(exc).__name__}: {exc}")
                raise

            if not suspended:
                # agent_complete audit, using self._totals()
            root_span.set_attribute("total_turns", turn_count)
```

6. After the span:

```python
        total_cost, total_tokens = self._totals()
        if suspended:
            assert self._pending is not None
            self.result = AgentResult(
                output="", run_id=run_id, status="suspended", pending_approvals=list(self._pending.approvals),
                turns=list(self._turns), total_cost_usd=total_cost, total_tokens=total_tokens,
                audit_root_hash=self._audit.root_hash() if self._audit else None, trace_id=self._tracer.trace_id,
            )
            return
        result = AgentResult(output=final_output, parsed=parsed, run_id=run_id, turns=self._turns, ...)
        await self._checkpoint("completed", turn_count, invalid_answers, native, result=result.model_dump(mode="json"))  # E
        # reporter run_complete + audit flush, as today
        self.result = result
```

New helpers:

```python
    def _snapshot(
        self, status: RunStatus, turn_count: int, invalid_answers: int, native: bool,
        result: dict[str, Any] | None = None,
    ) -> RunCheckpoint:
        now = datetime.utcnow()
        current = self._checkpointer.current if self._checkpointer else None
        return RunCheckpoint(
            run_id=self._run_id,
            version=current.version if current else 0,
            status=status,
            created_at=current.created_at if current else now,
            updated_at=now,
            prompt=self._prompt,
            context=self._context,
            output_type_name=self._output_type_name,
            messages=self._memory.history(),
            turns=list(self._turns),
            turn_count=turn_count,
            run_cost_usd=self._run_cost_usd,
            invalid_answers=invalid_answers,
            native_output=native,
            last_prompt_tokens=self._last_prompt_tokens,
            last_prompt_chars=self._last_prompt_chars,
            audit_events=self._audit.events() if self._audit else [],
            pending=self._pending.model_copy(deep=True) if self._pending else None,
            result=result,
        )

    async def _checkpoint(
        self, status: RunStatus, turn_count: int, invalid_answers: int, native: bool,
        result: dict[str, Any] | None = None,
    ) -> None:
        if self._checkpointer is not None:
            await self._checkpointer.save(self._snapshot(status, turn_count, invalid_answers, native, result))

    async def _resolve_tools(
        self, turn_number: int, pending: PendingTurn, answers: dict[str, bool]
    ) -> list[PendingApproval]:
        """Resolve every tool call of a pending turn concurrently; return approvals still waiting."""
        order = {tc.call_id: i for i, tc in enumerate(pending.turn.tool_calls)}

        async def resolve(tc: ToolCall) -> None:
            if tc.call_id in pending.results:
                return
            approval = next((a for a in pending.approvals if a.call_id == tc.call_id), None)
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
                        "approval_denied", tc.tool_name,
                        {"call_id": tc.call_id, "timed_out": False, "error": None, "via": "resume"},
                    )
                    reason = self._deny_tool(self._tool_context(tc, turn_number), "before_tool", "approval denied")
                    result = ToolResult(call_id=tc.call_id, tool_name=tc.tool_name, output=None,
                                        error=f"Tool call denied: {reason}")
            elif tc.call_id in pending.started and not self._is_idempotent(tc.tool_name):
                self._audit_event("tool_interrupted", tc.tool_name, {"call_id": tc.call_id})
                result = ToolResult(call_id=tc.call_id, tool_name=tc.tool_name, output=None,
                                    error="Tool call interrupted before completion; not retried")
            else:
                result = await self._run_tool(tc, turn_number)
            if result is None:
                pending.approvals.append(self._suspended.pop(tc.call_id))
            else:
                pending.results[tc.call_id] = result

        await asyncio.gather(*(resolve(tc) for tc in pending.turn.tool_calls))
        pending.approvals.sort(key=lambda a: order[a.call_id])
        return list(pending.approvals)

    def _record_tool_results(self, pending: PendingTurn) -> None:
        """Audit and add the turn's tool results to memory, in call order."""
        for tc in pending.turn.tool_calls:
            tool_result = pending.results[tc.call_id]
            # existing tool_call audit, tool message, and turn.tool_results.append(tool_result)

    def _is_idempotent(self, tool_name: str) -> bool:
        try:
            return self._registry.get(tool_name).schema.idempotent
        except Exception:
            return False

    def _tool_context(self, tc: ToolCall, turn: int) -> ToolCallContext:
        return ToolCallContext(run_id=self._run_id, turn=turn, tool_name=tc.tool_name,
                               arguments=dict(tc.arguments), call_id=tc.call_id, context=self._context)
```

`_gate_tool` uses `_tool_context`. `_run_tool(self, tc, turn, gate: bool = True) -> ToolResult | None`:

```python
                denial = await self._gate_tool(tc, turn) if gate else None
                if tc.call_id in self._suspended:
                    return None  # parked until resume answers it
                if denial is not None:
                    tool_result = failed(f"Tool call denied: {denial}")
                else:
                    await self._mark_started(tc.call_id)
                    try:
                        tool_result = await tool(call_id=tc.call_id, **tc.arguments)
                    ...
```

```python
    async def _mark_started(self, call_id: str) -> None:
        if self._pending is not None:
            self._pending.started.append(call_id)
        if self._checkpointer is not None:
            await self._checkpointer.mark_started(call_id)  # B
```

`_request_approval`, after the `approval_requested` audit:

```python
        if self._approver is SUSPEND:
            self._suspended[ctx.call_id] = PendingApproval(
                call_id=ctx.call_id, tool_name=ctx.tool_name, arguments=dict(ctx.arguments),
                reason=reason, turn=ctx.turn,
            )
            return None
```

(the existing `self._approver(request)` call needs `assert callable(self._approver)` / a cast for mypy.)

- [ ] **Step 5: Gates** — all suites, including `test_hooks.py` and `test_typed_results.py`.
- [ ] **Step 6: Commit** `feat: durable runs — checkpoints, suspension for approval, resume and crash recovery`.

---

### Task 4: Live verification (scratchpad)

- [ ] `e2e-durable/suspend.py` (process A): `OllamaProvider("llama3.2")`, `refund` tool behind `require_approval`, `approver=SUSPEND`, `SQLiteRunStore("runs.db")`; run with `run_id="live-1"`; print status and pending call ids; exit.
- [ ] `e2e-durable/approve.py` (process B): same Agent, `await agent.resume("live-1", approvals={<id>: True})`; print status, output, refund executions (appended to a file), `agent.audit.verify()`.
- [ ] `e2e-durable/kill.py`: a `slow_refund` (non-idempotent) tool that writes a marker then `await asyncio.sleep(30)`; run in a subprocess, `SIGKILL` it once the marker exists; then resume in-process and confirm the model receives the interrupted error and the tool marker count is 1.

---

### Task 5: Docs

**Files:** `README.md` ("Durable runs" section after "Hooks and approval gates"), `CHANGELOG.md` (Added; Fixed: totals leaked across runs), `examples/durable_approval.py` + `examples/README.md` row, `specs/06-harness-roadmap.md` (2.5 ticked; table "Durable runs / resume | ✅ (2.5)"), `specs/15-durable-runs.md` (status implemented), `PROJECT_INDEX.md` (`agent_kit/durable/`, tests, spec 15).

- [ ] `examples/durable_approval.py`:

```python
"""Durable approvals: suspend a refund for human review, approve it later from another process.

    ANTHROPIC_API_KEY=... python examples/durable_approval.py start
    ANTHROPIC_API_KEY=... python examples/durable_approval.py approve <call_id>
"""

import asyncio
import sys

from agent_kit import SUSPEND, Agent, AgentConfig, tool
from agent_kit.durable import SQLiteRunStore
from agent_kit.hooks import Hooks, require_approval
from agent_kit.providers import AnthropicProvider

RUN_ID = "ticket-9913"


@tool(description="Refund an order in full")
async def refund(order_id: str) -> dict:
    return {"order_id": order_id, "refunded": True}


def build_agent() -> Agent:
    return Agent(
        AnthropicProvider(),
        tools=[refund],
        config=AgentConfig(
            run_store=SQLiteRunStore("runs.db"),
            hooks=Hooks(before_tool=[require_approval("refund", reason="refunds need a human")]),
            approver=SUSPEND,
        ),
    )


async def main() -> None:
    agent = build_agent()
    if sys.argv[1:2] == ["start"]:
        result = await agent.run("Customer on ticket 9913 wants order A-1001 refunded.", run_id=RUN_ID)
        for pending in result.pending_approvals:
            print(f"waiting for approval: {pending.tool_name}({pending.arguments}) — call id {pending.call_id}")
    else:
        result = await agent.resume(RUN_ID, approvals={sys.argv[2]: True})
        print(result.status, "-", result.output)


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] Gates + `python3 -m py_compile examples/*.py`; commit `docs: durable runs guide and example`; fast-forward `main`, push, watch CI.
