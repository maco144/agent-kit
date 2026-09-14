# Harness Adapters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Report Claude Agent SDK and OpenAI Agents SDK runs to agent-kit Cloud — audit trail, fleet metrics, alerts — with no server change.

**Architecture:** A harness-neutral `RunRecorder` owns one `AuditChain` per run and emits the six existing `CloudEvent` types through a new thread-safe `CloudReporter.submit_threadsafe`. `ClaudeAgentObserver` feeds it from Claude Agent SDK hooks plus the message stream; `AgentKitTraceProcessor` feeds it from OpenAI Agents SDK tracing spans.

**Tech Stack:** Python 3.11+, agent-kit SDK internals (`AuditChain`, `CloudReporter`, provider pricing), `claude-agent-sdk>=0.2`, `openai-agents>=0.22`, pytest + pytest-asyncio (`asyncio_mode = "auto"`).

**Spec:** `specs/07-harness-adapters.md`

## Global Constraints

- No server changes. Emitted events must match the payloads `server/app/routers/ingest.py` already reads.
- `run_id` values are UUID strings (server columns are `String(36)`). OpenAI trace IDs (`trace_<32 hex>`, 38 chars) are mapped with `uuid5`.
- Adapters observe only: hook callbacks return `{}`; messages pass through unchanged; nothing raises into the host harness except the harness's own exceptions from `observe()`.
- Only audit payload hashes and a prompt hash leave the process.
- `ruff check agent_kit tests`, `mypy agent_kit` (strict), `pytest` clean on Python 3.11 and 3.12, with and without the two extras installed.
- Tests never reach the network: integration tests replace `submit_threadsafe`; reporter tests drain their queues.

## Spec amendments found while planning

Recorded in `specs/07-harness-adapters.md` in Task 5:
1. `RunRecorder.start` takes `agent_name: str | None = None` (per-run harness name, e.g. OpenAI workflow name). Precedence: reporter `agent_name` → `start(agent_name=...)` → recorder default.
2. OpenAI `input_tokens` already include cached tokens, so OpenAI turns pass no cache split.
3. OpenAI `run_id = uuid5(namespace, trace_id)`; the raw `trace_id` goes in run metadata.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `agent_kit/cloud/reporter.py` | `project` / `agent_name` properties, `submit_threadsafe`, `audit_flush_payload` helper | Modify |
| `agent_kit/integrations/__init__.py` | Package docstring; no imports (adapters load lazily) | Create |
| `agent_kit/integrations/recorder.py` | `RunRecorder` | Create |
| `agent_kit/integrations/claude_agent_sdk.py` | `ClaudeAgentObserver` | Create |
| `agent_kit/integrations/openai_agents.py` | `AgentKitTraceProcessor`, `run_id_for_trace` | Create |
| `tests/conftest.py` | `cloud_capture` fixture (captures events, verifies flushed chains) | Modify |
| `tests/test_cloud_reporter.py` | `submit_threadsafe` tests | Modify |
| `tests/test_integrations_recorder.py` | Recorder behaviour | Create |
| `tests/test_integrations_claude.py` | Claude adapter (fakes + real-type smoke) | Create |
| `tests/test_integrations_openai_agents.py` | OpenAI adapter via real `agents.tracing` | Create |
| `pyproject.toml`, `.github/workflows/ci.yml` | Extras, mypy override, CI installs extras | Modify |
| `examples/claude_agent_sdk_monitored.py`, `examples/openai_agents_monitored.py` | Runnable examples | Create |
| `README.md`, `docs/cloud-quickstart.md`, `examples/README.md`, `CHANGELOG.md`, `specs/06-harness-roadmap.md`, `specs/07-harness-adapters.md`, `PROJECT_INDEX.md` | Docs | Modify |

---

### Task 1: Thread-safe event submission on `CloudReporter`

**Files:**
- Modify: `agent_kit/cloud/reporter.py`
- Test: `tests/test_cloud_reporter.py`

**Interfaces:**
- Produces: `CloudReporter.project -> str`, `CloudReporter.agent_name -> str`, `CloudReporter.submit_threadsafe(event: CloudEvent) -> None`, module function `audit_flush_payload(events: list[AuditEventRecord], final_root_hash: str) -> dict[str, Any]`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_cloud_reporter.py
import asyncio


def _drain(reporter: CloudReporter) -> list[CloudEvent]:
    """Empty the queue so the atexit flush has nothing to send."""
    drained = []
    while not reporter._queue.empty():
        drained.append(reporter._queue.get_nowait())
    return drained


def _event(run_id: str) -> CloudEvent:
    return CloudEvent(event_type=EventType.RUN_START, run_id=run_id, agent_name="a", project="p")


async def test_submit_threadsafe_from_worker_thread_reaches_loop_queue():
    reporter = make_reporter(flush_interval_s=3600)
    reporter.submit_threadsafe(_event("on-loop"))
    await asyncio.to_thread(reporter.submit_threadsafe, _event("off-loop"))
    await asyncio.sleep(0)

    assert [e.run_id for e in _drain(reporter)] == ["on-loop", "off-loop"]
    assert reporter._flush_task is not None
    reporter._flush_task.cancel()


def test_submit_threadsafe_without_a_loop_queues_for_later_flush():
    reporter = make_reporter()
    reporter.submit_threadsafe(_event("no-loop"))
    assert [e.run_id for e in _drain(reporter)] == ["no-loop"]


def test_reporter_exposes_project_and_agent_name():
    reporter = make_reporter(project="billing", agent_name="assistant")
    assert (reporter.project, reporter.agent_name) == ("billing", "assistant")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cloud_reporter.py -k "submit_threadsafe or exposes" -v`
Expected: FAIL — `AttributeError: 'CloudReporter' object has no attribute 'submit_threadsafe'`

- [ ] **Step 3: Implement**

In `CloudReporter.__init__`, after `self._http = None`:

```python
        self._loop: asyncio.AbstractEventLoop | None = None
```

Add properties after `__init__`:

```python
    @property
    def project(self) -> str:
        return self._project

    @property
    def agent_name(self) -> str:
        return self._agent_name
```

Replace the body of `on_audit_flush` payload construction with the helper:

```python
            payload=audit_flush_payload(events, final_root_hash),
```

Add after `close()` in the "Manual controls" section:

```python
    def submit_threadsafe(self, event: CloudEvent) -> None:
        """
        Enqueue an event from synchronous code on any thread. Never raises.

        Used by harness adapters whose callbacks are synchronous. On the reporter's
        event-loop thread the event is queued directly; from any other thread it is
        handed to that loop. Before a loop has started, it waits in the queue for the
        next flush or the exit-time flush.
        """
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        loop = self._loop
        if loop is not None and loop.is_running() and running is not loop:
            loop.call_soon_threadsafe(self.submit_threadsafe, event)
            return
        if running is not None:
            self._ensure_flush_task()
        self._put(event)
```

Replace `_enqueue` and record the loop in `_ensure_flush_task`:

```python
    async def _enqueue(self, event: CloudEvent) -> None:
        self._ensure_flush_task()
        self._put(event)

    def _put(self, event: CloudEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.debug(
                "agent-kit Cloud: event queue full, dropping %s", event.event_type
            )

    def _ensure_flush_task(self) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._loop = loop
        ...  # rest unchanged
```

Module-level helper, next to `_encode_batch`:

```python
def audit_flush_payload(events: list[AuditEventRecord], final_root_hash: str) -> dict[str, Any]:
    """The audit_flush payload the ingest API verifies: every chain link, hashes only."""
    return {
        "final_root_hash": final_root_hash,
        "event_count": len(events),
        "events": [
            {
                "event_id": e.event_id,
                "event_type": e.event_type,
                "actor": e.actor,
                "payload_hash": e.payload_hash,
                "prev_root": e.prev_root,
                "leaf_hash": e.leaf_hash,
                "timestamp": e.timestamp.isoformat(),
            }
            for e in events
        ],
    }
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_cloud_reporter.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent_kit/cloud/reporter.py tests/test_cloud_reporter.py
git commit -m "feat: thread-safe CloudReporter.submit_threadsafe for harness adapters"
```

---

### Task 2: `RunRecorder`

**Files:**
- Create: `agent_kit/integrations/__init__.py`, `agent_kit/integrations/recorder.py`
- Modify: `tests/conftest.py`
- Create: `tests/test_integrations_recorder.py`

**Interfaces:**
- Consumes: `CloudReporter.submit_threadsafe`, `.project`, `.agent_name`, `audit_flush_payload` (Task 1); `AuditChain`; `anthropic._estimate_cost(model, in, out, cache_read, cache_write)`, `openai._estimate_cost(model, in, out)`.
- Produces: `RunRecorder(reporter, harness, agent_name=None)` with `start(run_id, model, prompt, metadata=None, agent_name=None)`, `llm_turn(run_id, model, input_tokens, output_tokens, cache_read_tokens=0, cache_write_tokens=0, tool_names=None, duration_ms=0)`, `tool_call(run_id, call_id, tool_name, success, error=None, duration_ms=0)`, `audit(run_id, event_type, actor, payload)`, `complete(run_id, num_turns=None, harness_cost_usd=None)`, `error(run_id, error_type, message)`. Fixture `cloud_capture` with `.reporter`, `.events`, `.types()`, `.of(event_type)`, `.audit_types(run_id)`, `.assert_chain_intact(run_id)`.

- [ ] **Step 1: Add the capture fixture and write the failing tests**

```python
# append to tests/conftest.py
import hashlib

from agent_kit.cloud.models import CloudEvent
from agent_kit.cloud.reporter import CloudReporter


class CloudCapture:
    """Collects CloudEvents a reporter would send; verifies flushed audit chains."""

    def __init__(self, reporter: CloudReporter) -> None:
        self.reporter = reporter
        self.events: list[CloudEvent] = []

    def types(self) -> list[str]:
        return [e.event_type.value for e in self.events]

    def of(self, event_type: str) -> list[CloudEvent]:
        return [e for e in self.events if e.event_type.value == event_type]

    def flush_payload(self, run_id: str) -> dict[str, Any]:
        (flush,) = [e for e in self.of("audit_flush") if e.run_id == run_id]
        return flush.payload

    def audit_types(self, run_id: str) -> list[str]:
        return [e["event_type"] for e in self.flush_payload(run_id)["events"]]

    def assert_chain_intact(self, run_id: str) -> None:
        """Re-derive every link exactly as server/app/audit_chain.py does."""
        payload = self.flush_payload(run_id)
        root = "0" * 64
        for e in payload["events"]:
            expected = hashlib.sha256(
                (root + e["event_type"] + e["payload_hash"] + e["timestamp"]).encode()
            ).hexdigest()
            assert e["prev_root"] == root
            assert e["leaf_hash"] == expected
            root = e["leaf_hash"]
        assert root == payload["final_root_hash"]
        assert payload["event_count"] == len(payload["events"])


@pytest.fixture
def cloud_capture(monkeypatch):
    reporter = CloudReporter(api_key="akt_test", project="proj")
    capture = CloudCapture(reporter)
    monkeypatch.setattr(reporter, "submit_threadsafe", capture.events.append)
    return capture
```

```python
# tests/test_integrations_recorder.py
from __future__ import annotations

import threading

import pytest

from agent_kit.integrations.recorder import RunRecorder

RUN = "3b1e7d1c-0000-4000-8000-000000000001"


def test_lifecycle_emits_native_event_sequence(cloud_capture):
    rec = RunRecorder(cloud_capture.reporter, harness="test-harness")

    rec.start(RUN, model="claude-opus-5", prompt="hi", metadata={"session_id": "s1"})
    rec.llm_turn(RUN, "claude-opus-5", input_tokens=1000, output_tokens=100, tool_names=["Read"])
    rec.tool_call(RUN, "tu_1", "Read", success=True, duration_ms=12)
    rec.llm_turn(RUN, "claude-opus-5", input_tokens=500, output_tokens=50, cache_read_tokens=1000)
    rec.complete(RUN)

    assert cloud_capture.types() == ["run_start", "turn_complete", "turn_complete", "run_complete", "audit_flush"]
    start = cloud_capture.of("run_start")[0].payload
    assert start["model"] == "claude-opus-5"
    assert start["harness"] == "test-harness"
    assert start["session_id"] == "s1"
    assert len(start["prompt_hash"]) == 64

    first, second = (e.payload for e in cloud_capture.of("turn_complete"))
    assert (first["turn_index"], first["tool_names"]) == (0, ["Read"])
    assert first["cost_usd"] == pytest.approx((1000 * 5 + 100 * 25) / 1_000_000)
    assert second["cost_usd"] == pytest.approx((500 * 5 + 50 * 25 + 1000 * 5 * 0.1) / 1_000_000)

    done = cloud_capture.of("run_complete")[0].payload
    assert done["total_turns"] == 2
    assert done["total_tokens"] == 1000 + 100 + 500 + 50 + 1000
    assert done["total_cost_usd"] == pytest.approx(first["cost_usd"] + second["cost_usd"])
    assert done["audit_root_hash"] == cloud_capture.flush_payload(RUN)["final_root_hash"]

    assert cloud_capture.audit_types(RUN) == [
        "agent_start", "llm_complete", "tool_call", "llm_complete", "agent_complete",
    ]
    cloud_capture.assert_chain_intact(RUN)
    assert {e.agent_name for e in cloud_capture.events} == {"test-harness"}
    assert {e.project for e in cloud_capture.events} == {"proj"}


def test_run_start_waits_for_a_model(cloud_capture):
    rec = RunRecorder(cloud_capture.reporter, harness="h")
    rec.start(RUN, model=None, prompt=None)
    assert cloud_capture.events == []

    rec.llm_turn(RUN, "claude-sonnet-5", input_tokens=10, output_tokens=5)

    assert cloud_capture.types() == ["run_start", "turn_complete"]
    assert cloud_capture.of("run_start")[0].payload["model"] == "claude-sonnet-5"


def test_run_start_sent_at_completion_when_no_turns(cloud_capture):
    rec = RunRecorder(cloud_capture.reporter, harness="h")
    rec.start(RUN, model=None, prompt=None)
    rec.complete(RUN)
    assert cloud_capture.types() == ["run_start", "run_complete", "audit_flush"]


def test_harness_cost_is_reconciled_into_turn_costs(cloud_capture):
    rec = RunRecorder(cloud_capture.reporter, harness="h")
    rec.start(RUN, model="claude-opus-5", prompt="p")
    rec.llm_turn(RUN, "claude-opus-5", input_tokens=1000, output_tokens=100)  # $0.0075
    rec.complete(RUN, num_turns=3, harness_cost_usd=0.01)

    turns = [e.payload for e in cloud_capture.of("turn_complete")]
    assert turns[1]["reconciliation"] is True
    assert turns[1]["cost_usd"] == pytest.approx(0.0025)
    assert (turns[1]["input_tokens"], turns[1]["output_tokens"]) == (0, 0)
    assert sum(t["cost_usd"] for t in turns) == pytest.approx(0.01)
    done = cloud_capture.of("run_complete")[0].payload
    assert (done["total_cost_usd"], done["total_turns"]) == (pytest.approx(0.01), 3)


def test_no_reconciliation_when_costs_agree(cloud_capture):
    rec = RunRecorder(cloud_capture.reporter, harness="h")
    rec.start(RUN, model="claude-opus-5", prompt="p")
    rec.llm_turn(RUN, "claude-opus-5", input_tokens=1000, output_tokens=100)
    rec.complete(RUN, harness_cost_usd=0.0075)
    assert len(cloud_capture.of("turn_complete")) == 1


def test_error_emits_run_error_and_flushes_chain(cloud_capture):
    rec = RunRecorder(cloud_capture.reporter, harness="h")
    rec.start(RUN, model="claude-opus-5", prompt="p")
    rec.llm_turn(RUN, "claude-opus-5", input_tokens=1, output_tokens=1)
    rec.error(RUN, "RateLimit", "x" * 900)

    assert cloud_capture.types() == ["run_start", "turn_complete", "run_error", "audit_flush"]
    err = cloud_capture.of("run_error")[0].payload
    assert (err["error_type"], len(err["error_message"]), err["turn_count"]) == ("RateLimit", 500, 1)
    assert cloud_capture.audit_types(RUN)[-1] == "agent_error"
    cloud_capture.assert_chain_intact(RUN)


def test_calls_for_unknown_or_finished_runs_are_dropped(cloud_capture):
    rec = RunRecorder(cloud_capture.reporter, harness="h")
    rec.llm_turn("nope", "claude-opus-5", 1, 1)
    rec.tool_call("nope", "c", "t", success=True)
    rec.complete("nope")
    rec.start(RUN, model="claude-opus-5", prompt="p")
    rec.complete(RUN)
    rec.complete(RUN)
    assert cloud_capture.types() == ["run_start", "run_complete", "audit_flush"]


def test_start_is_idempotent(cloud_capture):
    rec = RunRecorder(cloud_capture.reporter, harness="h")
    rec.start(RUN, model="claude-opus-5", prompt="p")
    rec.start(RUN, model="claude-opus-5", prompt="p")
    rec.complete(RUN)
    assert cloud_capture.types().count("run_start") == 1
    assert cloud_capture.audit_types(RUN).count("agent_start") == 1


def test_agent_name_precedence(cloud_capture, monkeypatch):
    rec = RunRecorder(cloud_capture.reporter, harness="h", agent_name="default-name")
    rec.start("r1", model="m", prompt=None)
    rec.start("r2", model="m", prompt=None, agent_name="workflow-name")
    monkeypatch.setattr(type(cloud_capture.reporter), "agent_name", property(lambda self: "configured"))
    rec.start("r3", model="m", prompt=None, agent_name="workflow-name")

    names = {e.run_id: e.agent_name for e in cloud_capture.of("run_start")}
    assert names == {"r1": "default-name", "r2": "workflow-name", "r3": "configured"}


def test_recorder_never_raises(cloud_capture, monkeypatch):
    def boom(event):
        raise RuntimeError("reporter down")

    monkeypatch.setattr(cloud_capture.reporter, "submit_threadsafe", boom)
    rec = RunRecorder(cloud_capture.reporter, harness="h")
    rec.start(RUN, model="claude-opus-5", prompt="p")
    rec.llm_turn(RUN, "claude-opus-5", 1, 1)
    rec.complete(RUN)


def test_tool_calls_from_many_threads_keep_the_chain_intact(cloud_capture):
    rec = RunRecorder(cloud_capture.reporter, harness="h")
    rec.start(RUN, model="claude-opus-5", prompt="p")
    threads = [
        threading.Thread(target=rec.tool_call, args=(RUN, f"c{i}", "t"), kwargs={"success": True})
        for i in range(16)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rec.complete(RUN)

    assert cloud_capture.audit_types(RUN).count("tool_call") == 16
    cloud_capture.assert_chain_intact(RUN)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_integrations_recorder.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent_kit.integrations'`

- [ ] **Step 3: Implement**

```python
# agent_kit/integrations/__init__.py
"""
Adapters that report other agent harnesses to agent-kit Cloud.

- ``agent_kit.integrations.claude_agent_sdk`` — Claude Agent SDK (``pip install agent-kit[claude-agent-sdk]``)
- ``agent_kit.integrations.openai_agents`` — OpenAI Agents SDK (``pip install agent-kit[openai-agents]``)

Nothing is imported here, so installing agent-kit never requires either harness.
"""
```

```python
# agent_kit/integrations/recorder.py
"""RunRecorder — turn another harness's activity into agent-kit runs, audit chains, and Cloud events."""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agent_kit.audit.chain import AuditChain
from agent_kit.cloud.models import CloudEvent, EventType
from agent_kit.cloud.reporter import audit_flush_payload

if TYPE_CHECKING:
    from agent_kit.cloud.reporter import CloudReporter

logger = logging.getLogger("agent_kit.integrations")

_RECONCILE_THRESHOLD_USD = 0.000001


@dataclass
class _Run:
    agent_name: str
    model: str | None
    prompt_hash: str
    metadata: dict[str, Any]
    chain: AuditChain = field(default_factory=AuditChain)
    run_start_sent: bool = False
    turns: int = 0
    total_tokens: int = 0
    priced_cost_usd: float = 0.0


def _price(
    model: str | None, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int
) -> float:
    """USD for one model call from agent-kit's pricing tables; 0.0 when unpriced."""
    if not model:
        return 0.0
    try:
        if model.startswith("claude"):
            from agent_kit.providers.anthropic import _estimate_cost as anthropic_cost

            return anthropic_cost(model, input_tokens, output_tokens, cache_read, cache_write)
        from agent_kit.providers.openai import _estimate_cost as openai_cost

        return openai_cost(model, input_tokens, output_tokens)
    except ImportError:
        return 0.0


class RunRecorder:
    """
    Harness-neutral run lifecycle for adapters.

    Adapters call ``start``, then ``llm_turn`` / ``tool_call`` / ``audit`` as the harness
    works, and finish with ``complete`` or ``error``. The recorder keeps one AuditChain per
    run and emits the same CloudEvents a native agent-kit Agent does, so audit, fleet
    metrics, and alerting work unchanged. Every method is synchronous, thread-safe, and
    never raises.
    """

    def __init__(
        self, reporter: CloudReporter, harness: str, agent_name: str | None = None
    ) -> None:
        self._reporter = reporter
        self._harness = harness
        self._default_agent_name = agent_name or harness
        self._runs: dict[str, _Run] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(
        self,
        run_id: str,
        model: str | None,
        prompt: str | None,
        metadata: dict[str, Any] | None = None,
        agent_name: str | None = None,
    ) -> None:
        with self._guard("start"):
            if run_id in self._runs:
                return
            run = _Run(
                agent_name=self._reporter.agent_name or agent_name or self._default_agent_name,
                model=model,
                prompt_hash=hashlib.sha256((prompt or "").encode()).hexdigest(),
                metadata=dict(metadata or {}),
            )
            self._runs[run_id] = run
            run.chain.append(
                "agent_start",
                actor=run_id,
                payload={**run.metadata, "harness": self._harness, "prompt_hash": run.prompt_hash},
            )
            if model:
                self._send_run_start(run_id, run)

    def llm_turn(
        self,
        run_id: str,
        model: str | None,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        tool_names: list[str] | None = None,
        duration_ms: int = 0,
    ) -> None:
        with self._guard("llm_turn"):
            run = self._runs.get(run_id)
            if run is None:
                logger.debug("RunRecorder.llm_turn for unknown run %s dropped", run_id)
                return
            if not run.run_start_sent:
                run.model = run.model or model
                self._send_run_start(run_id, run)
            resolved_model = model or run.model
            cost = _price(
                resolved_model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
            )
            names = list(tool_names or [])
            run.chain.append(
                "llm_complete",
                actor=self._harness,
                payload={
                    "model": resolved_model,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_tokens": cache_read_tokens,
                    "cache_write_tokens": cache_write_tokens,
                    "has_tool_calls": bool(names),
                },
            )
            self._send(
                run_id,
                run,
                EventType.TURN_COMPLETE,
                {
                    "turn_index": run.turns,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cost_usd": cost,
                    "duration_ms": duration_ms,
                    "tool_names": names,
                },
            )
            run.turns += 1
            run.total_tokens += input_tokens + output_tokens + cache_read_tokens + cache_write_tokens
            run.priced_cost_usd += cost

    def tool_call(
        self,
        run_id: str,
        call_id: str,
        tool_name: str,
        success: bool,
        error: str | None = None,
        duration_ms: int = 0,
    ) -> None:
        self.audit(
            run_id,
            "tool_call",
            actor=tool_name,
            payload={
                "call_id": call_id,
                "success": success,
                "error": error,
                "duration_ms": duration_ms,
            },
        )

    def audit(self, run_id: str, event_type: str, actor: str, payload: dict[str, Any]) -> None:
        with self._guard("audit"):
            run = self._runs.get(run_id)
            if run is None:
                logger.debug("RunRecorder.audit(%s) for unknown run %s dropped", event_type, run_id)
                return
            run.chain.append(event_type, actor=actor, payload=payload)

    def complete(
        self, run_id: str, num_turns: int | None = None, harness_cost_usd: float | None = None
    ) -> None:
        with self._guard("complete"):
            run = self._runs.pop(run_id, None)
            if run is None:
                logger.debug("RunRecorder.complete for unknown run %s dropped", run_id)
                return
            if not run.run_start_sent:
                self._send_run_start(run_id, run)

            total_cost = run.priced_cost_usd
            if harness_cost_usd is not None:
                delta = harness_cost_usd - run.priced_cost_usd
                if abs(delta) > _RECONCILE_THRESHOLD_USD:
                    # Fleet metrics sum turn costs, so the harness's authoritative total
                    # has to arrive as a turn.
                    self._send(
                        run_id,
                        run,
                        EventType.TURN_COMPLETE,
                        {
                            "turn_index": run.turns,
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "cost_usd": delta,
                            "duration_ms": 0,
                            "tool_names": [],
                            "reconciliation": True,
                        },
                    )
                total_cost = harness_cost_usd

            turns = run.turns if num_turns is None else num_turns
            run.chain.append(
                "agent_complete",
                actor=run_id,
                payload={"turns": turns, "total_tokens": run.total_tokens, "total_cost_usd": total_cost},
            )
            self._send(
                run_id,
                run,
                EventType.RUN_COMPLETE,
                {
                    "total_cost_usd": total_cost,
                    "total_tokens": run.total_tokens,
                    "total_turns": turns,
                    "audit_root_hash": run.chain.root_hash(),
                },
            )
            self._flush_audit(run_id, run)

    def error(self, run_id: str, error_type: str, message: str) -> None:
        with self._guard("error"):
            run = self._runs.pop(run_id, None)
            if run is None:
                logger.debug("RunRecorder.error for unknown run %s dropped", run_id)
                return
            if not run.run_start_sent:
                self._send_run_start(run_id, run)
            truncated = message[:500]
            run.chain.append(
                "agent_error",
                actor=run_id,
                payload={"error_type": error_type, "error_message": truncated},
            )
            self._send(
                run_id,
                run,
                EventType.RUN_ERROR,
                {"error_type": error_type, "error_message": truncated, "turn_count": run.turns},
            )
            self._flush_audit(run_id, run)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @contextmanager
    def _guard(self, operation: str) -> Iterator[None]:
        with self._lock:
            try:
                yield
            except Exception:
                logger.debug("RunRecorder.%s failed", operation, exc_info=True)

    def _send_run_start(self, run_id: str, run: _Run) -> None:
        run.run_start_sent = True
        self._send(
            run_id,
            run,
            EventType.RUN_START,
            {
                **run.metadata,
                "model": run.model,
                "prompt_hash": run.prompt_hash,
                "harness": self._harness,
            },
        )

    def _flush_audit(self, run_id: str, run: _Run) -> None:
        self._send(
            run_id,
            run,
            EventType.AUDIT_FLUSH,
            audit_flush_payload(run.chain.events(), run.chain.root_hash()),
        )

    def _send(self, run_id: str, run: _Run, event_type: EventType, payload: dict[str, Any]) -> None:
        self._reporter.submit_threadsafe(
            CloudEvent(
                event_type=event_type,
                run_id=run_id,
                agent_name=run.agent_name,
                project=self._reporter.project,
                payload=payload,
            )
        )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_integrations_recorder.py -v && ruff check agent_kit tests && mypy agent_kit`
Expected: PASS / clean

- [ ] **Step 5: Commit**

```bash
git add agent_kit/integrations tests/conftest.py tests/test_integrations_recorder.py
git commit -m "feat: RunRecorder — harness-neutral runs, audit chains, and Cloud events"
```

---

### Task 3: Claude Agent SDK adapter

**Files:**
- Create: `agent_kit/integrations/claude_agent_sdk.py`
- Create: `tests/test_integrations_claude.py`

**Interfaces:**
- Consumes: `RunRecorder` (Task 2), `cloud_capture` fixture.
- Produces: `ClaudeAgentObserver(reporter, agent_name=None)` with `.hooks() -> dict[str, list[HookMatcher]]`, `.with_hooks(options) -> options`, `.observe(messages, prompt=None) -> AsyncIterator[message]`; module constant `HARNESS = "claude-agent-sdk"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_integrations_claude.py
"""Claude Agent SDK adapter.

Most tests drive the observer with dataclass fakes named like the SDK's message types
(the adapter dispatches on type name). The smoke tests at the bottom use the real SDK.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from typing import Any

import pytest

from agent_kit.integrations.claude_agent_sdk import ClaudeAgentObserver


@dataclass
class SystemMessage:
    subtype: str
    data: dict[str, Any]


@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class AssistantMessage:
    content: list[Any]
    model: str
    usage: dict[str, Any] | None = None
    message_id: str | None = None
    session_id: str | None = None


@dataclass
class ResultMessage:
    subtype: str = "success"
    is_error: bool = False
    num_turns: int = 2
    session_id: str = "s1"
    total_cost_usd: float | None = None
    errors: list[str] | None = None


INIT = SystemMessage("init", {"session_id": "s1", "model": "claude-opus-5", "tools": ["Read"]})


def usage(inp: int, out: int, cache_read: int = 0, cache_write: int = 0) -> dict[str, int]:
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write,
    }


def hook(event: str, **fields: Any) -> dict[str, Any]:
    return {"hook_event_name": event, "session_id": "s1", "cwd": "/", "transcript_path": "", **fields}


async def collect(observer: ClaudeAgentObserver, source: Any, prompt: str | None = "find it") -> list[Any]:
    return [m async for m in observer.observe(source, prompt=prompt)]


async def test_observe_records_a_full_run_and_passes_messages_through(cloud_capture):
    observer = ClaudeAgentObserver(cloud_capture.reporter)
    messages = [
        INIT,
        AssistantMessage([TextBlock("Looking.")], "claude-opus-5", usage(900, 10), "msg_1", "s1"),
        AssistantMessage(
            [ToolUseBlock("tu_1", "Read", {"path": "a"})], "claude-opus-5", usage(1000, 100), "msg_1", "s1"
        ),
        AssistantMessage([TextBlock("Found.")], "claude-opus-5", usage(200, 20, cache_read=1000), "msg_2", "s1"),
        ResultMessage(num_turns=2, total_cost_usd=0.05),
    ]

    async def source():
        yield messages[0]
        yield messages[1]
        yield messages[2]
        await observer._on_hook(hook("PreToolUse", tool_name="Read", tool_use_id="tu_1", tool_input={}), "tu_1", None)
        await observer._on_hook(
            hook("PostToolUse", tool_name="Read", tool_use_id="tu_1", tool_input={}, tool_response="ok"), "tu_1", None
        )
        yield messages[3]
        yield messages[4]

    seen = await collect(observer, source())

    assert seen == messages
    assert cloud_capture.types() == [
        "run_start", "turn_complete", "turn_complete", "turn_complete", "run_complete", "audit_flush",
    ]
    (run_id,) = {e.run_id for e in cloud_capture.events}
    start = cloud_capture.of("run_start")[0]
    assert start.payload["model"] == "claude-opus-5"
    assert start.payload["harness"] == "claude-agent-sdk"
    assert start.payload["session_id"] == "s1"
    assert start.agent_name == "claude-agent"

    turn1, turn2, reconcile = (e.payload for e in cloud_capture.of("turn_complete"))
    assert (turn1["input_tokens"], turn1["output_tokens"], turn1["tool_names"]) == (1000, 100, ["Read"])
    assert (turn2["input_tokens"], turn2["tool_names"]) == (200, [])
    assert reconcile["reconciliation"] is True
    assert turn1["cost_usd"] + turn2["cost_usd"] + reconcile["cost_usd"] == pytest.approx(0.05)

    done = cloud_capture.of("run_complete")[0].payload
    assert (done["total_turns"], done["total_cost_usd"]) == (2, pytest.approx(0.05))
    assert cloud_capture.audit_types(run_id) == [
        "agent_start", "llm_complete", "tool_call", "llm_complete", "agent_complete",
    ]
    cloud_capture.assert_chain_intact(run_id)


async def test_tool_failure_hook_records_unsuccessful_call(cloud_capture):
    observer = ClaudeAgentObserver(cloud_capture.reporter)

    async def source():
        yield INIT
        await observer._on_hook(hook("PreToolUse", tool_name="Bash", tool_use_id="tu_9", tool_input={}), "tu_9", None)
        result = await observer._on_hook(
            hook("PostToolUseFailure", tool_name="Bash", tool_use_id="tu_9", tool_input={}, error="exit 1", is_interrupt=False),
            "tu_9",
            None,
        )
        assert result == {}
        yield ResultMessage(total_cost_usd=None)

    await collect(observer, source())

    (run_id,) = {e.run_id for e in cloud_capture.events}
    assert "tool_call" in cloud_capture.audit_types(run_id)
    cloud_capture.assert_chain_intact(run_id)


async def test_subagent_and_compaction_hooks_become_audit_events(cloud_capture):
    observer = ClaudeAgentObserver(cloud_capture.reporter)

    async def source():
        yield INIT
        await observer._on_hook(hook("SubagentStart", agent_id="a1", agent_type="researcher"), None, None)
        await observer._on_hook(
            hook("SubagentStop", agent_id="a1", agent_type="researcher", stop_hook_active=False, agent_transcript_path=""),
            None,
            None,
        )
        await observer._on_hook(hook("PreCompact", trigger="auto", custom_instructions=None), None, None)
        yield ResultMessage()

    await collect(observer, source())

    (run_id,) = {e.run_id for e in cloud_capture.events}
    assert cloud_capture.audit_types(run_id) == [
        "agent_start", "subagent_start", "subagent_stop", "context_compaction", "agent_complete",
    ]


async def test_hooks_for_unobserved_sessions_are_ignored(cloud_capture):
    observer = ClaudeAgentObserver(cloud_capture.reporter)
    result = await observer._on_hook(hook("PostToolUse", tool_name="Read", tool_use_id="x", tool_input={}), "x", None)
    assert result == {}
    assert cloud_capture.events == []


async def test_harness_exception_is_recorded_and_reraised_unchanged(cloud_capture):
    observer = ClaudeAgentObserver(cloud_capture.reporter)
    failure = ConnectionError("CLI exited")

    async def source():
        yield INIT
        raise failure

    with pytest.raises(ConnectionError) as info:
        await collect(observer, source())

    assert info.value is failure
    assert cloud_capture.types() == ["run_start", "run_error", "audit_flush"]
    err = cloud_capture.of("run_error")[0].payload
    assert (err["error_type"], err["error_message"]) == ("ConnectionError", "CLI exited")


async def test_stream_ending_without_result_is_an_incomplete_run(cloud_capture):
    observer = ClaudeAgentObserver(cloud_capture.reporter)

    async def source():
        yield INIT

    await collect(observer, source())

    assert cloud_capture.of("run_error")[0].payload["error_type"] == "IncompleteRun"


async def test_error_result_records_run_error(cloud_capture):
    observer = ClaudeAgentObserver(cloud_capture.reporter)

    async def source():
        yield INIT
        yield ResultMessage(subtype="error_max_turns", is_error=True, errors=["hit max turns"])

    await collect(observer, source())

    err = cloud_capture.of("run_error")[0].payload
    assert (err["error_type"], err["error_message"]) == ("ResultError", "hit max turns")


async def test_consumer_breaking_after_result_does_not_add_an_error(cloud_capture):
    observer = ClaudeAgentObserver(cloud_capture.reporter)

    async def source():
        yield INIT
        yield ResultMessage()
        yield SystemMessage("trailing", {"session_id": "s1"})

    async for message in observer.observe(source()):
        if isinstance(message, ResultMessage):
            break

    assert cloud_capture.types() == ["run_start", "run_complete", "audit_flush"]


async def test_each_observe_call_is_a_separate_run(cloud_capture):
    observer = ClaudeAgentObserver(cloud_capture.reporter)

    async def source():
        yield INIT
        yield ResultMessage()

    await collect(observer, source())
    await collect(observer, source())

    assert len({e.run_id for e in cloud_capture.of("run_complete")}) == 2


# ---------------------------------------------------------------------------
# Real SDK types
# ---------------------------------------------------------------------------

requires_claude_sdk = pytest.mark.skipif(
    importlib.util.find_spec("claude_agent_sdk") is None, reason="claude-agent-sdk extra not installed"
)


@requires_claude_sdk
def test_with_hooks_keeps_user_hooks():
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

    async def user_hook(input_data: Any, tool_use_id: str | None, context: Any) -> dict[str, Any]:
        return {}

    from agent_kit.cloud.reporter import CloudReporter

    observer = ClaudeAgentObserver(CloudReporter(api_key="akt_test"))
    options = observer.with_hooks(ClaudeAgentOptions(hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[user_hook])]}))

    assert options.hooks is not None
    assert options.hooks["PreToolUse"][0].hooks == [user_hook]
    assert len(options.hooks["PreToolUse"]) == 2
    assert set(options.hooks) >= {"PreToolUse", "PostToolUse", "PostToolUseFailure", "SubagentStart", "SubagentStop", "PreCompact"}


@requires_claude_sdk
async def test_observe_with_real_sdk_message_types(cloud_capture):
    from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock, ToolUseBlock

    observer = ClaudeAgentObserver(cloud_capture.reporter)
    (post_tool_use,) = observer.hooks()["PostToolUse"]

    async def source():
        yield SystemMessage(subtype="init", data={"session_id": "real", "model": "claude-sonnet-5"})
        yield AssistantMessage(
            content=[TextBlock(text="hi"), ToolUseBlock(id="tu", name="Read", input={})],
            model="claude-sonnet-5",
            usage=usage(100, 10),
            message_id="m1",
            session_id="real",
        )
        await post_tool_use.hooks[0](
            {"hook_event_name": "PostToolUse", "session_id": "real", "tool_name": "Read", "tool_use_id": "tu",
             "tool_input": {}, "tool_response": "", "cwd": "/", "transcript_path": ""},
            "tu",
            {"signal": None},
        )
        yield ResultMessage(
            subtype="success", duration_ms=10, duration_api_ms=8, is_error=False, num_turns=1,
            session_id="real", total_cost_usd=0.0003,
        )

    await collect(observer, source(), prompt="hi")

    (run_id,) = {e.run_id for e in cloud_capture.events}
    assert cloud_capture.types()[-2:] == ["run_complete", "audit_flush"]
    assert "tool_call" in cloud_capture.audit_types(run_id)
    cloud_capture.assert_chain_intact(run_id)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_integrations_claude.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent_kit.integrations.claude_agent_sdk'`

- [ ] **Step 3: Implement**

```python
# agent_kit/integrations/claude_agent_sdk.py
"""
Claude Agent SDK adapter — report Claude Agent SDK runs to agent-kit Cloud.

Usage::

    from claude_agent_sdk import ClaudeAgentOptions, query
    from agent_kit.cloud import CloudReporter
    from agent_kit.integrations.claude_agent_sdk import ClaudeAgentObserver

    observer = ClaudeAgentObserver(CloudReporter(project="support"))
    options = observer.with_hooks(ClaudeAgentOptions(allowed_tools=["Read", "Grep"]))

    async for message in observer.observe(query(prompt=prompt, options=options), prompt=prompt):
        ...  # every message arrives unchanged

Each ``observe()`` call is one agent-kit run. Hooks only observe: they return no
decision and never change tool input or output. Hook events for a session that isn't
being observed are ignored, because cost and completion come from the message stream.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import AsyncIterable, AsyncIterator
from typing import TYPE_CHECKING, Any

from agent_kit.integrations.recorder import RunRecorder

if TYPE_CHECKING:
    from agent_kit.cloud.reporter import CloudReporter

logger = logging.getLogger("agent_kit.integrations")

HARNESS = "claude-agent-sdk"
_HOOK_EVENTS = (
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
)


class ClaudeAgentObserver:
    """Observes Claude Agent SDK runs through hooks and the message stream."""

    def __init__(self, reporter: CloudReporter, agent_name: str | None = None) -> None:
        self._recorder = RunRecorder(reporter, harness=HARNESS, agent_name=agent_name or "claude-agent")
        self._sessions: dict[str, _Observation] = {}  # session_id -> active observe() call
        self._tool_started: dict[str, float] = {}  # tool_use_id -> monotonic start
        self._lock = threading.Lock()

    def hooks(self) -> dict[str, list[Any]]:
        """Hook matchers for ``ClaudeAgentOptions.hooks``. Prefer ``with_hooks`` to merge them."""
        from claude_agent_sdk import HookMatcher

        return {event: [HookMatcher(hooks=[self._on_hook])] for event in _HOOK_EVENTS}

    def with_hooks(self, options: Any) -> Any:
        """Add agent-kit's hooks to ``options``, after any hooks already configured."""
        merged: dict[str, list[Any]] = {
            event: list(matchers) for event, matchers in (options.hooks or {}).items()
        }
        for event, matchers in self.hooks().items():
            merged.setdefault(event, []).extend(matchers)
        options.hooks = merged
        return options

    async def observe(
        self, messages: AsyncIterable[Any], prompt: str | None = None
    ) -> AsyncIterator[Any]:
        """Yield every message from ``messages`` unchanged while recording the run."""
        run = _Observation(self, str(uuid.uuid4()), prompt)
        try:
            async for message in messages:
                run.on_message(message)
                yield message
        except Exception as exc:
            run.fail(type(exc).__name__, str(exc))
            raise
        finally:
            run.close()

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    async def _on_hook(
        self, input_data: Any, tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        try:
            self._record_hook(input_data, tool_use_id)
        except Exception:
            logger.debug("ClaudeAgentObserver hook failed", exc_info=True)
        return {}

    def _record_hook(self, data: dict[str, Any], tool_use_id: str | None) -> None:
        event = data.get("hook_event_name")
        with self._lock:
            observation = self._sessions.get(data.get("session_id") or "")
        if observation is None:
            logger.debug("Claude hook %s for unobserved session ignored", event)
            return
        # Hooks fire after the model response that triggered them has fully arrived, so
        # close out that turn first to keep the audit chain in causal order.
        observation.flush_turn()
        run_id = observation.run_id

        call_id = tool_use_id or data.get("tool_use_id") or ""
        if event == "PreToolUse":
            with self._lock:
                self._tool_started[call_id] = time.monotonic()
        elif event in ("PostToolUse", "PostToolUseFailure"):
            with self._lock:
                t0 = self._tool_started.pop(call_id, None)
            failed = event == "PostToolUseFailure"
            self._recorder.tool_call(
                run_id,
                call_id,
                data.get("tool_name") or "",
                success=not failed,
                error=str(data.get("error")) if failed else None,
                duration_ms=int((time.monotonic() - t0) * 1000) if t0 is not None else 0,
            )
        elif event in ("SubagentStart", "SubagentStop"):
            self._recorder.audit(
                run_id,
                "subagent_start" if event == "SubagentStart" else "subagent_stop",
                actor=data.get("agent_type") or "subagent",
                payload={"agent_id": data.get("agent_id")},
            )
        elif event == "PreCompact":
            self._recorder.audit(
                run_id, "context_compaction", actor=HARNESS, payload={"trigger": data.get("trigger")}
            )

    def _bind(self, session_id: str, observation: _Observation) -> None:
        with self._lock:
            self._sessions[session_id] = observation

    def _unbind(self, session_id: str, observation: _Observation) -> None:
        with self._lock:
            if self._sessions.get(session_id) is observation:
                del self._sessions[session_id]


class _Observation:
    """State for one ``observe()`` call — one agent-kit run."""

    def __init__(self, observer: ClaudeAgentObserver, run_id: str, prompt: str | None) -> None:
        self._observer = observer
        self._recorder = observer._recorder
        self.run_id = run_id
        self._prompt = prompt
        self._started = False
        self._finished = False
        self._session_id: str | None = None
        # One API response can arrive as several AssistantMessages sharing a message_id;
        # accumulate them and record a single turn.
        self._turn_id: str | None = None
        self._turn_model: str | None = None
        self._turn_usage: dict[str, Any] | None = None
        self._turn_tools: list[str] = []
        self._turn_open = False

    def on_message(self, message: Any) -> None:
        try:
            self._record(message)
        except Exception:
            logger.debug("ClaudeAgentObserver failed to record %s", type(message).__name__, exc_info=True)

    def _record(self, message: Any) -> None:
        kind = type(message).__name__
        data = getattr(message, "data", None) if kind == "SystemMessage" else None
        session_id = data.get("session_id") if isinstance(data, dict) else getattr(message, "session_id", None)

        if not self._started:
            model = data.get("model") if isinstance(data, dict) else getattr(message, "model", None)
            self._recorder.start(
                self.run_id,
                model=model,
                prompt=self._prompt,
                metadata={"session_id": session_id} if session_id else None,
            )
            self._started = True
        if session_id and session_id != self._session_id:
            if self._session_id:
                self._observer._unbind(self._session_id, self)
            self._session_id = session_id
            self._observer._bind(session_id, self)

        if kind == "AssistantMessage":
            if message.message_id is None or message.message_id != self._turn_id:
                self.flush_turn()
                self._turn_id = message.message_id
                self._turn_model = message.model
                self._turn_open = True
            if message.usage:
                self._turn_usage = message.usage
            self._turn_tools.extend(
                block.name for block in message.content if type(block).__name__ == "ToolUseBlock"
            )
        elif kind == "ResultMessage":
            self.flush_turn()
            if message.is_error:
                detail = "; ".join(message.errors or []) or message.subtype
                self._recorder.error(self.run_id, "ResultError", detail)
            else:
                self._recorder.complete(
                    self.run_id, num_turns=message.num_turns, harness_cost_usd=message.total_cost_usd
                )
            self._finished = True

    def flush_turn(self) -> None:
        if not self._turn_open:
            return
        usage = self._turn_usage or {}
        self._recorder.llm_turn(
            self.run_id,
            self._turn_model,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
            cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
            tool_names=self._turn_tools,
        )
        self._turn_id = None
        self._turn_model = None
        self._turn_usage = None
        self._turn_tools = []
        self._turn_open = False

    def fail(self, error_type: str, message: str) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            self.flush_turn()
            if not self._started:
                self._recorder.start(self.run_id, model=None, prompt=self._prompt)
                self._started = True
            self._recorder.error(self.run_id, error_type, message)
        except Exception:
            logger.debug("ClaudeAgentObserver failed to record run error", exc_info=True)

    def close(self) -> None:
        if self._started and not self._finished:
            self.fail("IncompleteRun", "message stream ended without a result")
        if self._session_id:
            self._observer._unbind(self._session_id, self)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_integrations_claude.py -v && ruff check agent_kit tests && mypy agent_kit`
Expected: PASS / clean (real-SDK tests skipped when the extra is absent)

- [ ] **Step 5: Commit**

```bash
git add agent_kit/integrations/claude_agent_sdk.py tests/test_integrations_claude.py
git commit -m "feat: Claude Agent SDK adapter for agent-kit Cloud"
```

---

### Task 4: OpenAI Agents SDK adapter

**Files:**
- Create: `agent_kit/integrations/openai_agents.py`
- Create: `tests/test_integrations_openai_agents.py`
- Modify: `pyproject.toml` (mypy override — required for `mypy` to pass without the extra)

**Interfaces:**
- Consumes: `RunRecorder` (Task 2), `cloud_capture` fixture.
- Produces: `AgentKitTraceProcessor(reporter, agent_name=None)` (a `TracingProcessor`), `run_id_for_trace(trace_id: str) -> str`, `HARNESS = "openai-agents"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_integrations_openai_agents.py
"""OpenAI Agents SDK adapter, driven through the SDK's real tracing machinery (no network)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace as NS

import pytest

tracing = pytest.importorskip("agents.tracing")

from agent_kit.integrations.openai_agents import AgentKitTraceProcessor, run_id_for_trace  # noqa: E402


@pytest.fixture
def processor(cloud_capture):
    proc = AgentKitTraceProcessor(cloud_capture.reporter)
    tracing.set_trace_processors([proc])
    yield proc
    tracing.set_trace_processors([])


def test_run_ids_are_uuids_derived_from_trace_ids():
    trace_id = "trace_" + "a" * 32
    assert run_id_for_trace(trace_id) == run_id_for_trace(trace_id)
    assert str(uuid.UUID(run_id_for_trace(trace_id))) == run_id_for_trace(trace_id)
    assert len(run_id_for_trace(trace_id)) == 36


def test_trace_becomes_a_run_with_turns_tools_and_audit(cloud_capture, processor):
    with tracing.trace("support-workflow", group_id="conv-1") as trace:
        with tracing.agent_span("triage"):
            with tracing.generation_span(model="gpt-4o", usage={"input_tokens": 1000, "output_tokens": 100}):
                pass
            with tracing.function_span("lookup_order", input='{"id": 1}'):
                pass
            with tracing.function_span("refund", input="{}") as failing:
                failing.set_error({"message": "payment API down", "data": None})
            with tracing.guardrail_span("pii_filter", triggered=True):
                pass
            with tracing.handoff_span(from_agent="triage", to_agent="billing"):
                pass
            with tracing.generation_span(model="gpt-4o-mini", usage={"input_tokens": 50, "output_tokens": 5}):
                pass

    run_id = run_id_for_trace(trace.trace_id)
    assert cloud_capture.types() == [
        "run_start", "turn_complete", "turn_complete", "run_complete", "audit_flush",
    ]
    assert {e.run_id for e in cloud_capture.events} == {run_id}

    start = cloud_capture.of("run_start")[0]
    assert start.agent_name == "support-workflow"
    assert start.payload["model"] == "gpt-4o"
    assert start.payload["harness"] == "openai-agents"
    assert (start.payload["trace_id"], start.payload["group_id"]) == (trace.trace_id, "conv-1")

    first, second = (e.payload for e in cloud_capture.of("turn_complete"))
    assert (first["input_tokens"], first["output_tokens"]) == (1000, 100)
    assert first["cost_usd"] == pytest.approx((1000 * 2.5 + 100 * 10) / 1_000_000)
    assert second["cost_usd"] == pytest.approx((50 * 0.15 + 5 * 0.6) / 1_000_000)

    assert cloud_capture.audit_types(run_id) == [
        "agent_start", "llm_complete", "tool_call", "tool_call", "guardrail", "handoff",
        "llm_complete", "agent_complete",
    ]
    cloud_capture.assert_chain_intact(run_id)


def test_agent_span_error_fails_the_run(cloud_capture, processor):
    with tracing.trace("wf") as trace:
        with tracing.agent_span("worker") as agent:
            agent.set_error({"message": "MaxTurnsExceeded", "data": None})

    assert cloud_capture.types() == ["run_start", "run_error", "audit_flush"]
    err = cloud_capture.of("run_error")[0].payload
    assert (err["error_type"], err["error_message"]) == ("AgentError", "MaxTurnsExceeded")
    cloud_capture.assert_chain_intact(run_id_for_trace(trace.trace_id))


def test_response_spans_use_response_usage(cloud_capture):
    proc = AgentKitTraceProcessor(cloud_capture.reporter)
    trace = NS(trace_id="trace_" + "b" * 32, name="wf", group_id=None)
    proc.on_trace_start(trace)
    span = NS(
        trace_id=trace.trace_id,
        span_id="span_1",
        error=None,
        started_at="2026-09-13T10:00:00+00:00",
        ended_at="2026-09-13T10:00:01.250000+00:00",
        span_data=NS(type="response", response=NS(model="gpt-4o", usage=NS(input_tokens=40, output_tokens=8)), usage=None),
    )
    proc.on_span_end(span)
    proc.on_trace_end(trace)

    turn = cloud_capture.of("turn_complete")[0].payload
    assert (turn["input_tokens"], turn["output_tokens"], turn["duration_ms"]) == (40, 8, 1250)


def test_processor_never_raises_on_malformed_spans(cloud_capture):
    proc = AgentKitTraceProcessor(cloud_capture.reporter)
    proc.on_span_end(NS(trace_id="t", span_id="s", error=None, started_at=None, ended_at=None, span_data=None))
    proc.on_trace_end(NS(trace_id="never-started", name="x", group_id=None))
    proc.shutdown()
    proc.force_flush()
    assert cloud_capture.events == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_integrations_openai_agents.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent_kit.integrations.openai_agents'` (or all skipped if `openai-agents` isn't installed — install it first: `pip install openai-agents`)

- [ ] **Step 3: Implement**

```python
# agent_kit/integrations/openai_agents.py
"""
OpenAI Agents SDK adapter — report OpenAI Agents SDK runs to agent-kit Cloud.

Usage::

    from agents import add_trace_processor
    from agent_kit.cloud import CloudReporter
    from agent_kit.integrations.openai_agents import AgentKitTraceProcessor

    add_trace_processor(AgentKitTraceProcessor(CloudReporter(project="support")))

Each trace becomes one agent-kit run; OpenAI's own trace exporter keeps running. Nothing
is recorded while Agents SDK tracing is disabled.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

try:
    from agents.tracing import TracingProcessor
except ImportError as e:
    raise ImportError(
        "AgentKitTraceProcessor requires the 'openai-agents' package. "
        "Install it with: pip install agent-kit[openai-agents]"
    ) from e

from agent_kit.integrations.recorder import RunRecorder

if TYPE_CHECKING:
    from agents.tracing import Span, Trace

    from agent_kit.cloud.reporter import CloudReporter

logger = logging.getLogger("agent_kit.integrations")

HARNESS = "openai-agents"
_RUN_ID_NAMESPACE = uuid.UUID("5d0f6a52-8c1e-4b7a-9f3d-2e6c1a4b8d70")


def run_id_for_trace(trace_id: str) -> str:
    """agent-kit run IDs are UUIDs; Agents SDK trace IDs are not. Derive one deterministically."""
    return str(uuid.uuid5(_RUN_ID_NAMESPACE, trace_id))


def _usage_value(usage: Any, key: str) -> int:
    if usage is None:
        return 0
    value = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
    return int(value or 0)


def _duration_ms(span: Any) -> int:
    try:
        started = datetime.fromisoformat(span.started_at)
        ended = datetime.fromisoformat(span.ended_at)
    except (TypeError, ValueError):
        return 0
    return max(0, int((ended - started).total_seconds() * 1000))


class AgentKitTraceProcessor(TracingProcessor):
    """Maps Agents SDK traces and spans onto agent-kit runs. Never raises into the SDK."""

    def __init__(self, reporter: CloudReporter, agent_name: str | None = None) -> None:
        self._recorder = RunRecorder(reporter, harness=HARNESS, agent_name=agent_name)
        self._agent_errors: dict[str, str] = {}  # trace_id -> agent span error message
        self._lock = threading.Lock()

    def on_trace_start(self, trace: Trace) -> None:
        try:
            metadata: dict[str, Any] = {"trace_id": trace.trace_id, "workflow": trace.name}
            group_id = getattr(trace, "group_id", None)
            if group_id:
                metadata["group_id"] = group_id
            self._recorder.start(
                run_id_for_trace(trace.trace_id),
                model=None,
                prompt=None,
                metadata=metadata,
                agent_name=trace.name,
            )
        except Exception:
            logger.debug("AgentKitTraceProcessor.on_trace_start failed", exc_info=True)

    def on_trace_end(self, trace: Trace) -> None:
        try:
            with self._lock:
                error = self._agent_errors.pop(trace.trace_id, None)
            run_id = run_id_for_trace(trace.trace_id)
            if error is None:
                self._recorder.complete(run_id)
            else:
                self._recorder.error(run_id, "AgentError", error)
        except Exception:
            logger.debug("AgentKitTraceProcessor.on_trace_end failed", exc_info=True)

    def on_span_start(self, span: Span[Any]) -> None:
        return None

    def on_span_end(self, span: Span[Any]) -> None:
        try:
            self._record_span(span)
        except Exception:
            logger.debug("AgentKitTraceProcessor.on_span_end failed", exc_info=True)

    def shutdown(self) -> None:
        return None

    def force_flush(self) -> None:
        return None

    def _record_span(self, span: Any) -> None:
        data = span.span_data
        run_id = run_id_for_trace(span.trace_id)
        kind = data.type

        if kind == "response":
            response = data.response
            usage = getattr(response, "usage", None) if response is not None else None
            self._recorder.llm_turn(
                run_id,
                getattr(response, "model", None),
                input_tokens=_usage_value(usage or data.usage, "input_tokens"),
                output_tokens=_usage_value(usage or data.usage, "output_tokens"),
                duration_ms=_duration_ms(span),
            )
        elif kind == "generation":
            self._recorder.llm_turn(
                run_id,
                data.model,
                input_tokens=_usage_value(data.usage, "input_tokens"),
                output_tokens=_usage_value(data.usage, "output_tokens"),
                duration_ms=_duration_ms(span),
            )
        elif kind == "function":
            error = span.error
            self._recorder.tool_call(
                run_id,
                span.span_id,
                data.name,
                success=error is None,
                error=error.get("message") if error else None,
                duration_ms=_duration_ms(span),
            )
        elif kind == "handoff":
            self._recorder.audit(
                run_id, "handoff", actor=data.from_agent or "agent", payload={"to_agent": data.to_agent}
            )
        elif kind == "guardrail":
            self._recorder.audit(
                run_id, "guardrail", actor=data.name, payload={"triggered": data.triggered}
            )
        elif kind == "agent" and span.error:
            with self._lock:
                self._agent_errors[span.trace_id] = span.error.get("message") or "agent error"
```

`pyproject.toml` — after `[tool.mypy]`:

```toml
[[tool.mypy.overrides]]
# TracingProcessor is Any when openai-agents isn't installed; subclassing it is intended.
module = "agent_kit.integrations.openai_agents"
disallow_subclassing_any = false
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_integrations_openai_agents.py -v && ruff check agent_kit tests && mypy agent_kit`
Expected: PASS / clean

- [ ] **Step 5: Commit**

```bash
git add agent_kit/integrations/openai_agents.py tests/test_integrations_openai_agents.py pyproject.toml
git commit -m "feat: OpenAI Agents SDK adapter for agent-kit Cloud"
```

---

### Task 5: Packaging, CI, examples, docs

**Files:**
- Modify: `pyproject.toml`, `.github/workflows/ci.yml`
- Create: `examples/claude_agent_sdk_monitored.py`, `examples/openai_agents_monitored.py`
- Modify: `README.md`, `docs/cloud-quickstart.md`, `examples/README.md`, `CHANGELOG.md`, `specs/06-harness-roadmap.md`, `specs/07-harness-adapters.md`, `PROJECT_INDEX.md`

- [ ] **Step 1: Extras and CI**

```toml
[project.optional-dependencies]
openai           = ["openai>=1.30"]
ollama           = []        # uses httpx (already core); listed for discoverability
otel             = ["opentelemetry-api>=1.24", "opentelemetry-sdk>=1.24"]
claude-agent-sdk = ["claude-agent-sdk>=0.2"]
openai-agents    = ["openai-agents>=0.22"]
all              = ["agent-kit[openai,ollama,otel,claude-agent-sdk,openai-agents]"]
```

`.github/workflows/ci.yml` SDK install step:

```yaml
        run: pip install -e ".[dev,openai,otel,claude-agent-sdk,openai-agents]"
```

- [ ] **Step 2: Examples**

```python
# examples/claude_agent_sdk_monitored.py
"""
Claude Agent SDK run reported to agent-kit Cloud.

Requires: pip install agent-kit[claude-agent-sdk], Claude Code authentication,
and AGENTKIT_API_KEY for the Cloud side.
"""

import asyncio

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from agent_kit.cloud import CloudReporter
from agent_kit.integrations.claude_agent_sdk import ClaudeAgentObserver


async def main() -> None:
    reporter = CloudReporter(project="demo", agent_name="repo-explorer")
    observer = ClaudeAgentObserver(reporter)
    options = observer.with_hooks(ClaudeAgentOptions(allowed_tools=["Read", "Glob", "Grep"], max_turns=6))

    prompt = "List the three largest Python modules in this repository and what each does."
    async for message in observer.observe(query(prompt=prompt, options=options), prompt=prompt):
        if isinstance(message, ResultMessage):
            print(message.result)
            print(f"turns={message.num_turns} cost=${message.total_cost_usd or 0:.4f}")

    await reporter.close()


if __name__ == "__main__":
    asyncio.run(main())
```

```python
# examples/openai_agents_monitored.py
"""
OpenAI Agents SDK run reported to agent-kit Cloud.

Requires: pip install agent-kit[openai-agents], OPENAI_API_KEY, and AGENTKIT_API_KEY.
"""

import asyncio

from agents import Agent, Runner, add_trace_processor, function_tool

from agent_kit.cloud import CloudReporter
from agent_kit.integrations.openai_agents import AgentKitTraceProcessor


@function_tool
def order_status(order_id: str) -> str:
    """Look up an order's shipping status."""
    return f"Order {order_id} shipped yesterday."


async def main() -> None:
    reporter = CloudReporter(project="demo")
    add_trace_processor(AgentKitTraceProcessor(reporter))

    agent = Agent(name="support", instructions="Answer order questions.", tools=[order_status])
    result = await Runner.run(agent, "Where is order 1042?")
    print(result.final_output)

    await reporter.close()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 3: Docs**

`README.md` — under `## agent-kit Cloud`, after the `CloudReporter options` table:

```markdown
### Already on another harness?

Keep it. Adapters report Claude Agent SDK and OpenAI Agents SDK runs to the same audit trail,
fleet dashboard, and alerts — hash-chained on your machine, no server changes:

```python
# Claude Agent SDK — pip install agent-kit[claude-agent-sdk]
observer = ClaudeAgentObserver(CloudReporter(project="support"))
options = observer.with_hooks(ClaudeAgentOptions(allowed_tools=["Read"]))
async for message in observer.observe(query(prompt=prompt, options=options), prompt=prompt):
    ...

# OpenAI Agents SDK — pip install agent-kit[openai-agents]
add_trace_processor(AgentKitTraceProcessor(CloudReporter(project="support")))
```
```

`docs/cloud-quickstart.md` — add a section `## Other harnesses` with the same two snippets, plus: runs carry `harness` in their `run_start` payload; Claude cost comes from the SDK's `total_cost_usd`, OpenAI cost from agent-kit's pricing tables; hooks without `observe()` record nothing.

`examples/README.md` — two table rows:

```markdown
| [`claude_agent_sdk_monitored.py`](claude_agent_sdk_monitored.py) | 33 | A Claude Agent SDK run reported to agent-kit Cloud through hooks and the message stream. | Claude Code auth; `AGENTKIT_API_KEY` |
| [`openai_agents_monitored.py`](openai_agents_monitored.py) | 34 | An OpenAI Agents SDK run reported to agent-kit Cloud through a trace processor. | `OPENAI_API_KEY`; `AGENTKIT_API_KEY` |
```

`CHANGELOG.md` `[Unreleased]` → `### Added`:

```markdown
- **Harness adapters for agent-kit Cloud.** `agent_kit.integrations.claude_agent_sdk.ClaudeAgentObserver` and `agent_kit.integrations.openai_agents.AgentKitTraceProcessor` report Claude Agent SDK and OpenAI Agents SDK runs — turns, tool calls, handoffs, guardrails, subagents, cost — with a client-side audit chain, against the existing server. New extras: `claude-agent-sdk`, `openai-agents`.
- `CloudReporter.submit_threadsafe(event)` for synchronous callers on any thread.
```

`specs/07-harness-adapters.md` — apply the three amendments listed at the top of this plan and set `Status: **implemented**`.

`specs/06-harness-roadmap.md` — 3.1 line: prefix `- [x] **3.1a Harness adapters**` for the Python SDK adapters and add `- [ ] **3.1b OTLP ingest**` beneath it.

`PROJECT_INDEX.md` — add `integrations/` to the tree (`recorder.py`, `claude_agent_sdk.py`, `openai_agents.py`), the three new test files to the SDK test table, and `specs/07-harness-adapters.md` to the docs table.

- [ ] **Step 4: Gates, with and without extras**

Run (extras installed): `pytest && ruff check agent_kit tests && mypy agent_kit && python -m compileall -q examples/`
Run (fresh venv, `pip install -e ".[dev,openai]"` only): `pytest && mypy agent_kit`
Expected: clean both ways; integration smoke tests skip without extras.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .github/workflows/ci.yml examples README.md docs/cloud-quickstart.md CHANGELOG.md specs PROJECT_INDEX.md
git commit -m "docs: harness adapter extras, CI, examples, and guides"
```
