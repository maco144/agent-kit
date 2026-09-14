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


def price_call(
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
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
    ) -> float:
        """Record one model call; returns its priced cost (0.0 if the run is unknown)."""
        cost = 0.0
        with self._guard("llm_turn"):
            run = self._runs.get(run_id)
            if run is None:
                logger.debug("RunRecorder.llm_turn for unknown run %s dropped", run_id)
                return 0.0
            if not run.run_start_sent:
                run.model = run.model or model
                self._send_run_start(run_id, run)
            resolved_model = model or run.model
            cost = price_call(
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
        return cost

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
