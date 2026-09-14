"""
Assemble OTLP GenAI spans into agent-kit runs.

One trace is one run. Spans extend a server-built audit chain and drive the same
ingest handlers SDK events use, so fleet metrics, alerting, and support context
see OTLP runs like any other. See specs/08-otlp-ingest.md for the lifecycle.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit_chain import GENESIS_ROOT, append_event, verify_chain
from app.models import ActiveRunCache, AuditEvent, AuditRun
from app.otlp.decode import RawSpan
from app.otlp.normalize import GenAISpan, normalize
from app.otlp.pricing import estimate_cost
from app.routers.ingest import _process_event

IDLE_TIMEOUT = timedelta(minutes=5)

_RUN_NAMESPACE = uuid.UUID("0b6f4d2a-7c1e-4e3b-9a4f-3d2c1b0a9e8f")
_EVENT_TYPES = {"llm": "llm_complete", "tool": "tool_call", "agent": "agent_invoke"}


def run_id_for_trace(trace_id: str) -> str:
    """Trace IDs are 32 hex chars; run IDs are UUIDs."""
    return str(uuid.uuid5(_RUN_NAMESPACE, trace_id))


@dataclass
class AssembleResult:
    runs_started: list[str] = field(default_factory=list)
    runs_finished: list[str] = field(default_factory=list)


async def assemble(
    spans: list[RawSpan], org_id: str, db: AsyncSession, now: datetime | None = None
) -> AssembleResult:
    """Finalize idle runs, then apply ``spans`` trace by trace in end-time order."""
    result = AssembleResult()
    current = now or datetime.utcnow()
    await _finalize_idle_runs(org_id, db, current, result)

    by_trace: dict[str, list[RawSpan]] = defaultdict(list)
    for span in sorted(spans, key=lambda s: s.end_ns):
        by_trace[span.trace_id].append(span)
    for trace_id, trace_spans in by_trace.items():
        await _apply_trace(trace_id, trace_spans, org_id, db, current, result)
    return result


# ---------------------------------------------------------------------------
# Traces
# ---------------------------------------------------------------------------


async def _apply_trace(
    trace_id: str,
    spans: list[RawSpan],
    org_id: str,
    db: AsyncSession,
    now: datetime,
    result: AssembleResult,
) -> None:
    run_id = run_id_for_trace(trace_id)
    normalized = [(span, normalize(span)) for span in spans]
    genai = [g for _, g in normalized if g is not None]

    run = await _get_run(db, run_id)
    if run is not None and run.org_id != org_id:
        return
    if run is None and not genai:
        return
    cache = await _get_cache(db, run_id) if run is not None else None

    late_events = False
    for span, g in normalized:
        if g is not None:
            if run is None:
                run, cache = await _start_run(run_id, trace_id, g, genai, org_id, db)
                result.runs_started.append(run_id)
            applied = await _apply_span(run, cache, g, db, now)
            late_events = late_events or (applied and run.completed_at is not None)
        if not span.parent_span_id and run is not None and run.completed_at is None:
            await _finish_run(run, cache, db, _timestamp(span.end_ns), root=span)
            cache = None
            result.runs_finished.append(run_id)
            late_events = False

    if run is not None and late_events:
        await _verify(run, db)


async def _start_run(
    run_id: str,
    trace_id: str,
    first: GenAISpan,
    batch: list[GenAISpan],
    org_id: str,
    db: AsyncSession,
) -> tuple[AuditRun, ActiveRunCache | None]:
    resource = first.raw.resource
    project = _str(resource.get("agentkit.project")) or "default"
    agent_name = _str(resource.get("service.name")) or "otel-agent"
    model = next((g.model for g in batch if g.kind == "llm" and g.model), "")
    started_at = _timestamp(min(g.raw.start_ns for g in batch))
    context = {
        "harness": first.convention,
        "trace_id": trace_id,
        "conversation_id": first.conversation_id,
    }

    run = AuditRun(
        org_id=org_id,
        project=project,
        agent_name=agent_name,
        run_id=run_id,
        genesis_root=GENESIS_ROOT,
        final_root_hash=GENESIS_ROOT,
        event_count=0,
        started_at=started_at,
        integrity="pending",
        chain_origin="ingest",
    )
    db.add(run)
    db.add(
        append_event(
            run,
            event_id=_event_id(run_id, "agent_start"),
            event_type="agent_start",
            actor=run_id,
            payload=context,
            timestamp=started_at,
        )
    )
    await _process_event(
        {
            "event_id": _event_id(run_id, "run_start"),
            "event_type": "run_start",
            "run_id": run_id,
            "agent_name": agent_name,
            "project": project,
            "occurred_at": started_at.isoformat(),
            "payload": {"model": model, "prompt_hash": "", **context},
        },
        org_id,
        db,
    )
    return run, await _get_cache(db, run_id)


# ---------------------------------------------------------------------------
# Spans
# ---------------------------------------------------------------------------


async def _apply_span(
    run: AuditRun, cache: ActiveRunCache | None, span: GenAISpan, db: AsyncSession, now: datetime
) -> bool:
    """Append the span's audit event; update metrics while the run is active. False if already applied."""
    event_type = _EVENT_TYPES[span.kind]
    event_id = _event_id(run.run_id, f"{span.raw.span_id}:{event_type}")
    if await _event_exists(db, event_id):
        return False

    cost = (
        estimate_cost(span.model, span.input_tokens, span.output_tokens, span.cache_read_tokens, span.cache_write_tokens)
        if span.kind == "llm"
        else 0.0
    )
    db.add(
        append_event(
            run,
            event_id=event_id,
            event_type=event_type,
            actor=_actor(span),
            payload=_audit_payload(span, cost),
            timestamp=_timestamp(span.raw.end_ns),
        )
    )
    if cache is None:  # run already finished: a late span extends the chain only
        return True

    cache.last_event_at = now
    if span.kind == "llm":
        if not cache.model and span.model:
            cache.model = span.model
        await _process_event(
            {
                "event_id": _event_id(run.run_id, f"{span.raw.span_id}:turn"),
                "event_type": "turn_complete",
                "run_id": run.run_id,
                "agent_name": cache.agent_name,
                "project": cache.project,
                "occurred_at": _timestamp(span.raw.end_ns).isoformat(),
                "payload": {
                    "turn_index": cache.turns_so_far,
                    "input_tokens": span.input_tokens,
                    "output_tokens": span.output_tokens,
                    "cost_usd": cost,
                    "duration_ms": span.duration_ms,
                    "tool_names": [],
                },
            },
            run.org_id,
            db,
        )
    elif span.kind == "agent":
        # Outer spans end after the spans they contain, so the latest agent span
        # seen is the outermost one so far: it names the run and decides failure.
        if span.name:
            run.agent_name = span.name
            cache.agent_name = span.name
        cache.failure_message = f"agent {span.name or 'span'} failed" if span.failed else None
    return True


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------


async def _finish_run(
    run: AuditRun,
    cache: ActiveRunCache | None,
    db: AsyncSession,
    finished_at: datetime,
    root: RawSpan | None,
) -> None:
    failure: tuple[str, str] | None = None
    if root is not None and root.status_error:
        failure = ("TraceError", root.status_message or f"root span {root.name!r} failed")
    elif cache is not None and cache.failure_message:
        failure = ("AgentError", cache.failure_message)

    turns = cache.turns_so_far if cache else 0
    cost = cache.cost_so_far_usd if cache else 0.0
    tokens = (cache.input_tokens + cache.output_tokens) if cache else 0
    base = {
        "run_id": run.run_id,
        "agent_name": run.agent_name,
        "project": run.project,
        "occurred_at": finished_at.isoformat(),
    }

    if failure is not None:
        error_type, message = failure
        message = message[:500]
        db.add(
            append_event(
                run,
                event_id=_event_id(run.run_id, "agent_error"),
                event_type="agent_error",
                actor=run.run_id,
                payload={"error_type": error_type, "error_message": message},
                timestamp=finished_at,
            )
        )
        await _process_event(
            {
                **base,
                "event_id": _event_id(run.run_id, "run_error"),
                "event_type": "run_error",
                "payload": {"error_type": error_type, "error_message": message, "turn_count": turns},
            },
            run.org_id,
            db,
        )
    else:
        db.add(
            append_event(
                run,
                event_id=_event_id(run.run_id, "agent_complete"),
                event_type="agent_complete",
                actor=run.run_id,
                payload={"turns": turns, "total_tokens": tokens, "total_cost_usd": cost},
                timestamp=finished_at,
            )
        )
        await _process_event(
            {
                **base,
                "event_id": _event_id(run.run_id, "run_complete"),
                "event_type": "run_complete",
                "payload": {
                    "total_cost_usd": cost,
                    "total_tokens": tokens,
                    "total_turns": turns,
                    "audit_root_hash": run.final_root_hash,
                },
            },
            run.org_id,
            db,
        )

    run.completed_at = run.completed_at or finished_at
    await _verify(run, db)


async def _finalize_idle_runs(
    org_id: str, db: AsyncSession, now: datetime, result: AssembleResult
) -> None:
    rows = await db.execute(
        select(ActiveRunCache, AuditRun)
        .join(AuditRun, AuditRun.run_id == ActiveRunCache.run_id)
        .where(
            ActiveRunCache.org_id == org_id,
            AuditRun.chain_origin == "ingest",
            ActiveRunCache.last_event_at < now - IDLE_TIMEOUT,
        )
    )
    for cache, run in rows.all():
        await _finish_run(run, cache, db, cache.last_event_at or now, root=None)
        result.runs_finished.append(run.run_id)


async def _verify(run: AuditRun, db: AsyncSession) -> None:
    events = list(
        (
            await db.execute(
                select(AuditEvent).where(AuditEvent.run_id == run.run_id).order_by(AuditEvent.seq)
            )
        ).scalars().all()
    )
    ok, *_ = verify_chain(events)
    run.integrity = "verified" if ok else "failed"
    for event in events:
        event.verified = ok
    if not ok:
        try:
            from app.alerting.evaluator import fire_audit_integrity_failure

            await fire_audit_integrity_failure(
                org_id=run.org_id, agent_name=run.agent_name, project=run.project, run_id=run.run_id, db=db
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_run(db: AsyncSession, run_id: str) -> AuditRun | None:
    return (await db.execute(select(AuditRun).where(AuditRun.run_id == run_id))).scalar_one_or_none()


async def _get_cache(db: AsyncSession, run_id: str) -> ActiveRunCache | None:
    return (
        await db.execute(select(ActiveRunCache).where(ActiveRunCache.run_id == run_id))
    ).scalar_one_or_none()


async def _event_exists(db: AsyncSession, event_id: str) -> bool:
    found = await db.execute(select(AuditEvent.id).where(AuditEvent.event_id == event_id))
    return found.first() is not None


def _event_id(run_id: str, key: str) -> str:
    return str(uuid.uuid5(uuid.UUID(run_id), key))


def _timestamp(ns: int) -> datetime:
    return datetime(1970, 1, 1) + timedelta(microseconds=ns // 1000)


def _actor(span: GenAISpan) -> str:
    if span.kind == "llm":
        return span.model or "llm"
    return span.name or span.kind


def _audit_payload(span: GenAISpan, cost: float) -> dict[str, Any]:
    return {
        "span_id": span.raw.span_id,
        "kind": span.kind,
        "name": span.name,
        "model": span.model,
        "input_tokens": span.input_tokens,
        "output_tokens": span.output_tokens,
        "cache_read_tokens": span.cache_read_tokens,
        "cache_write_tokens": span.cache_write_tokens,
        "cost_usd": cost,
        "tool_call_id": span.tool_call_id,
        "success": not span.failed,
        "duration_ms": span.duration_ms,
    }


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""
