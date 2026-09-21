"""CloudReporter — fire-and-forget event reporter for agent-kit Cloud."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import logging
import os
import time
from typing import TYPE_CHECKING, Any

import httpx

from agent_kit.cloud.models import CloudEvent, EventType
from agent_kit.hooks import flagged_payload

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agent_kit.cloud.budgets import BudgetGuard
    from agent_kit.types import AgentResult, AuditEventRecord, Finding, Turn

logger = logging.getLogger("agent_kit.cloud")

_INGEST_PATH = "/v1/events"
_MAX_BATCH = 200
_BACKOFF_S = (1.0, 2.0)  # waits between the three attempts to ship a batch
_DROP_WARNING_INTERVAL_S = 60.0


class CloudReporter:
    """
    Batches and ships agent lifecycle events to agent-kit Cloud.

    All reporting is fire-and-forget — errors are logged, never raised.
    Agent performance is never affected by cloud connectivity issues.

    Usage::

        from agent_kit.cloud import CloudReporter

        reporter = CloudReporter(
            api_key="akt_live_...",                    # or set AGENTKIT_API_KEY
            base_url="https://agentkit.example.com",   # your agent-kit Cloud server, or set AGENTKIT_BASE_URL
            project="production",
            agent_name="billing-assistant",
        )

        agent = Agent(
            provider=AnthropicProvider(),
            config=AgentConfig(cloud=reporter),
        )
    """

    def __init__(
        self,
        api_key: str | None = None,
        project: str = "default",
        agent_name: str | None = None,
        base_url: str | None = None,
        flush_interval_s: float = 5.0,
        max_queue_size: int = 1000,
        include_output: bool = False,
    ) -> None:
        resolved_key = api_key or os.environ.get("AGENTKIT_API_KEY", "")
        if not resolved_key:
            raise ValueError(
                "api_key is required. Pass it explicitly or set AGENTKIT_API_KEY."
            )
        resolved_url = base_url or os.environ.get("AGENTKIT_BASE_URL", "")
        if not resolved_url:
            raise ValueError(
                "base_url is required: there is no hosted agent-kit Cloud endpoint yet. Pass the URL of your "
                "agent-kit Cloud server (see docs/self-hosting.md) or set AGENTKIT_BASE_URL."
            )
        self._api_key = resolved_key
        self._project = project
        self._agent_name = agent_name or ""
        self._base_url = resolved_url.rstrip("/")
        self._flush_interval_s = flush_interval_s
        self._max_queue_size = max_queue_size
        self._include_output = include_output

        self._queue: asyncio.Queue[CloudEvent] = asyncio.Queue(maxsize=max_queue_size)
        self._flush_task: asyncio.Task[None] | None = None
        self._http: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._budget_guard: BudgetGuard | None = None
        self._dropped = 0
        self._last_drop_warning = 0.0

        import atexit
        atexit.register(self._flush_sync)

    @property
    def dropped_events(self) -> int:
        """Events lost so far: queue full, rejected by the server, or unshippable after retries."""
        return self._dropped

    @property
    def project(self) -> str:
        return self._project

    @property
    def agent_name(self) -> str:
        return self._agent_name

    @property
    def api_key(self) -> str:
        return self._api_key

    @property
    def base_url(self) -> str:
        return self._base_url

    def budget_guard(self, refresh_interval_s: float = 30.0, fail_closed: bool = False) -> BudgetGuard:
        """The reporter's shared BudgetGuard, created on first use with these settings."""
        if self._budget_guard is None:
            from agent_kit.cloud.budgets import BudgetGuard

            self._budget_guard = BudgetGuard(
                self, refresh_interval_s=refresh_interval_s, fail_closed=fail_closed
            )
        return self._budget_guard

    # ------------------------------------------------------------------
    # Lifecycle hooks — called by AgentLoop
    # ------------------------------------------------------------------

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

    async def on_turn_complete(
        self, run_id: str, turn: Turn, turn_index: int
    ) -> None:
        await self._enqueue(CloudEvent(
            event_type=EventType.TURN_COMPLETE,
            run_id=run_id,
            agent_name=self._agent_name,
            project=self._project,
            payload={
                "turn_index": turn_index,
                "input_tokens": turn.cost.input_tokens,
                "output_tokens": turn.cost.output_tokens,
                "cost_usd": turn.cost.cost_usd,
                "duration_ms": turn.duration_ms,
                "tool_names": [tc.tool_name for tc in turn.tool_calls],
            },
        ))

    async def on_run_complete(self, run_id: str, result: AgentResult[Any]) -> None:
        payload: dict[str, Any] = {
            "total_cost_usd": result.total_cost_usd,
            "total_tokens": result.total_tokens,
            "total_turns": len(result.turns),
            "audit_root_hash": result.audit_root_hash,
        }
        if self._include_output:
            payload["output_hash"] = _sha256(result.output)
        await self._enqueue(CloudEvent(
            event_type=EventType.RUN_COMPLETE,
            run_id=run_id,
            agent_name=self._agent_name,
            project=self._project,
            payload=payload,
        ))

    async def on_run_error(
        self, run_id: str, exc: BaseException, turn_count: int
    ) -> None:
        await self._enqueue(CloudEvent(
            event_type=EventType.RUN_ERROR,
            run_id=run_id,
            agent_name=self._agent_name,
            project=self._project,
            payload={
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:500],
                "turn_count": turn_count,
            },
        ))

    async def on_circuit_state_change(
        self,
        run_id: str,
        resource: str,
        prev_state: str,
        new_state: str,
        failure_count: int,
    ) -> None:
        await self._enqueue(CloudEvent(
            event_type=EventType.CIRCUIT_STATE_CHANGE,
            run_id=run_id,
            agent_name=self._agent_name,
            project=self._project,
            payload={
                "resource": resource,
                "prev_state": prev_state,
                "new_state": new_state,
                "failure_count": failure_count,
            },
        ))

    async def on_tool_output_flagged(
        self, run_id: str, tool_name: str, call_id: str, action: str, findings: Sequence[Finding]
    ) -> None:
        await self._enqueue(CloudEvent(
            event_type=EventType.TOOL_OUTPUT_FLAGGED,
            run_id=run_id,
            agent_name=self._agent_name,
            project=self._project,
            payload=flagged_payload(call_id, tool_name, action, findings),
        ))

    async def on_audit_flush(
        self,
        run_id: str,
        events: list[AuditEventRecord],
        final_root_hash: str,
    ) -> None:
        await self._enqueue(CloudEvent(
            event_type=EventType.AUDIT_FLUSH,
            run_id=run_id,
            agent_name=self._agent_name,
            project=self._project,
            payload=audit_flush_payload(events, final_root_hash),
        ))

    # ------------------------------------------------------------------
    # Manual controls
    # ------------------------------------------------------------------

    async def flush(self) -> None:
        """Manually flush buffered events. Useful in tests and shutdown handlers."""
        await self._flush()

    async def close(self) -> None:
        """Flush remaining events and close the HTTP client."""
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self._flush()
        if self._http:
            await self._http.aclose()
            self._http = None

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

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _enqueue(self, event: CloudEvent) -> None:
        self._ensure_flush_task()
        self._put(event)

    def _put(self, event: CloudEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped += 1
            now = time.monotonic()
            if self._dropped == 1 or now - self._last_drop_warning >= _DROP_WARNING_INTERVAL_S:
                self._last_drop_warning = now
                logger.warning(
                    "agent-kit Cloud: event queue full (max_queue_size=%d), dropping %s; %d events dropped so far",
                    self._max_queue_size,
                    event.event_type.value,
                    self._dropped,
                )

    def _ensure_flush_task(self) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._loop = loop
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
            )
        self._flush_task = loop.create_task(self._flush_loop(), name="agentkit-cloud-flush")

    async def _flush_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._flush_interval_s)
                await self._flush()
        except asyncio.CancelledError:
            await self._flush()

    def _take_batch(self) -> list[CloudEvent]:
        events: list[CloudEvent] = []
        try:
            while len(events) < _MAX_BATCH:
                events.append(self._queue.get_nowait())
        except asyncio.QueueEmpty:
            pass
        return events

    async def _flush(self) -> None:
        """Ship everything queued, one batch after another."""
        while events := self._take_batch():
            await self._ship(events)

    async def _ship(self, events: list[CloudEvent]) -> None:
        if self._http is None:
            return
        body = _encode_batch(events)
        error = ""
        for attempt in range(len(_BACKOFF_S) + 1):
            try:
                resp = await self._http.post(
                    f"{self._base_url}{_INGEST_PATH}",
                    content=body,
                    headers=_ingest_headers(self._api_key),
                )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code < 400:
                    return
                error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if not _retryable(resp.status_code):  # the same batch would be rejected again
                    self._lose(events, f"agent-kit Cloud rejected %d event(s), not retrying — {error}")
                    return
            if attempt < len(_BACKOFF_S):
                await asyncio.sleep(_BACKOFF_S[attempt])
        self._lose(events, f"agent-kit Cloud: dropped %d event(s) after {len(_BACKOFF_S) + 1} attempts — {error}")

    def _lose(self, events: list[CloudEvent], message: str) -> None:
        self._dropped += len(events)
        logger.warning(message, len(events))

    def _flush_sync(self) -> None:
        """atexit handler — drains the remaining queue with a synchronous HTTP client, one attempt per batch."""
        batches: list[list[CloudEvent]] = []
        while events := self._take_batch():
            batches.append(events)
        if not batches:
            return
        try:
            with httpx.Client(timeout=10.0) as client:
                for events in batches:
                    resp = client.post(
                        f"{self._base_url}{_INGEST_PATH}",
                        content=_encode_batch(events),
                        headers=_ingest_headers(self._api_key),
                    )
                    if resp.status_code >= 400:
                        self._lose(events, f"agent-kit Cloud: atexit flush lost %d event(s) — HTTP {resp.status_code}")
        except Exception as exc:
            logger.warning("agent-kit Cloud: atexit flush failed: %s", exc)

    def __repr__(self) -> str:
        return (
            f"CloudReporter(project={self._project!r}, "
            f"agent_name={self._agent_name!r}, "
            f"queue_size={self._queue.qsize()})"
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _retryable(status_code: int) -> bool:
    """Server errors, timeouts, and rate limits may pass on retry; other 4xx won't."""
    return status_code >= 500 or status_code in (408, 429)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


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


def _encode_batch(events: list[CloudEvent]) -> bytes:
    ndjson = "\n".join(e.model_dump_json() for e in events).encode()
    return gzip.compress(ndjson)


def _ingest_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/x-ndjson",
        "Content-Encoding": "gzip",
    }
