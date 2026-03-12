"""CloudReporter — fire-and-forget event reporter for agent-kit Cloud."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import logging
import os
from typing import TYPE_CHECKING, Any

import httpx

from agent_kit.cloud.models import CloudEvent, EventType

if TYPE_CHECKING:
    from agent_kit.types import AgentResult, AuditEventRecord, Turn

logger = logging.getLogger("agent_kit.cloud")

_DEFAULT_BASE_URL = "https://ingest.agentkit.io"
_INGEST_PATH = "/v1/events"
_MAX_BATCH = 200


class CloudReporter:
    """
    Batches and ships agent lifecycle events to agent-kit Cloud.

    All reporting is fire-and-forget — errors are logged, never raised.
    Agent performance is never affected by cloud connectivity issues.

    Usage::

        from agent_kit.cloud import CloudReporter

        reporter = CloudReporter(
            api_key="akt_live_...",     # or set AGENTKIT_API_KEY
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
        base_url: str = _DEFAULT_BASE_URL,
        flush_interval_s: float = 5.0,
        max_queue_size: int = 1000,
        include_output: bool = False,
    ) -> None:
        resolved_key = api_key or os.environ.get("AGENTKIT_API_KEY", "")
        if not resolved_key:
            raise ValueError(
                "api_key is required. Pass it explicitly or set AGENTKIT_API_KEY."
            )
        self._api_key = resolved_key
        self._project = project
        self._agent_name = agent_name or ""
        self._base_url = base_url.rstrip("/")
        self._flush_interval_s = flush_interval_s
        self._max_queue_size = max_queue_size
        self._include_output = include_output

        self._queue: asyncio.Queue[CloudEvent] = asyncio.Queue(maxsize=max_queue_size)
        self._flush_task: asyncio.Task[None] | None = None
        self._http: httpx.AsyncClient | None = None

        import atexit
        atexit.register(self._flush_sync)

    # ------------------------------------------------------------------
    # Lifecycle hooks — called by AgentLoop
    # ------------------------------------------------------------------

    async def on_run_start(self, run_id: str, model: str | None, prompt: str) -> None:
        await self._enqueue(CloudEvent(
            event_type=EventType.RUN_START,
            run_id=run_id,
            agent_name=self._agent_name,
            project=self._project,
            payload={"model": model, "prompt_hash": _sha256(prompt)},
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

    async def on_run_complete(self, run_id: str, result: AgentResult) -> None:
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
        self, run_id: str, exc: Exception, turn_count: int
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
            payload={
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
            },
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

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _enqueue(self, event: CloudEvent) -> None:
        self._ensure_flush_task()
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

    async def _flush(self) -> None:
        events: list[CloudEvent] = []
        try:
            while len(events) < _MAX_BATCH:
                events.append(self._queue.get_nowait())
        except asyncio.QueueEmpty:
            pass
        if not events:
            return
        await self._ship(events)

    async def _ship(self, events: list[CloudEvent]) -> None:
        if self._http is None:
            return
        body = _encode_batch(events)
        for attempt in range(3):
            try:
                resp = await self._http.post(
                    f"{self._base_url}{_INGEST_PATH}",
                    content=body,
                    headers=_ingest_headers(self._api_key),
                )
                resp.raise_for_status()
                return
            except Exception as exc:
                if attempt == 2:
                    logger.debug(
                        "agent-kit Cloud: failed to ship %d events after 3 attempts: %s",
                        len(events),
                        exc,
                    )
                else:
                    await asyncio.sleep(2.0 ** attempt)

    def _flush_sync(self) -> None:
        """atexit handler — drains remaining queue with a synchronous HTTP client."""
        events: list[CloudEvent] = []
        try:
            while len(events) < _MAX_BATCH:
                events.append(self._queue.get_nowait())
        except Exception:
            pass
        if not events:
            return
        try:
            with httpx.Client(timeout=10.0) as client:
                client.post(
                    f"{self._base_url}{_INGEST_PATH}",
                    content=_encode_batch(events),
                    headers=_ingest_headers(self._api_key),
                )
        except Exception as exc:
            logger.debug("agent-kit Cloud: atexit flush failed: %s", exc)

    def __repr__(self) -> str:
        return (
            f"CloudReporter(project={self._project!r}, "
            f"agent_name={self._agent_name!r}, "
            f"queue_size={self._queue.qsize()})"
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _encode_batch(events: list[CloudEvent]) -> bytes:
    ndjson = "\n".join(e.model_dump_json() for e in events).encode()
    return gzip.compress(ndjson)


def _ingest_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/x-ndjson",
        "Content-Encoding": "gzip",
    }
