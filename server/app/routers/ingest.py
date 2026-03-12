"""POST /v1/events — receive NDJSON event batches from the SDK."""

from __future__ import annotations

import gzip
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit_chain import verify_chain
from app.auth import get_current_org
from app.database import get_db
from app.models import (
    ActiveRunCache,
    AgentMetricSnapshot,
    AuditEvent,
    AuditRun,
    CircuitBreakerEvent,
    CloudEventLog,
    Organization,
)
from app.schemas import IngestResponse

router = APIRouter(prefix="/v1/events", tags=["ingest"])


@router.post("", response_model=IngestResponse, status_code=status.HTTP_202_ACCEPTED)
async def ingest_events(
    request: Request,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> IngestResponse:
    """
    Receive a gzip-compressed NDJSON batch of CloudEvents from the SDK.

    Each line is one JSON-encoded CloudEvent. Events are:
    1. Stored in cloud_event_log (raw, all types).
    2. For audit_flush events: audit run + events are upserted, then
       Merkle chain is verified asynchronously (via background task).
    """
    body = await request.body()

    # Decompress if gzip
    content_encoding = request.headers.get("content-encoding", "")
    if content_encoding == "gzip":
        try:
            body = gzip.decompress(body)
        except Exception:
            raise HTTPException(status_code=400, detail="Failed to decompress gzip body.")

    # Parse NDJSON
    raw_events: list[dict] = []
    for line in body.decode().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw_events.append(json.loads(line))
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail=f"Invalid JSON line: {line[:120]}")

    if not raw_events:
        return IngestResponse(accepted=0, message="no events")

    accepted = 0
    for raw in raw_events:
        try:
            await _process_event(raw, org.id, db)
            accepted += 1
        except Exception:
            # Skip malformed individual events; don't fail the whole batch.
            continue

    await db.commit()

    # Trigger async verification for any audit runs received in this batch
    audit_run_ids = {
        e["payload"].get("run_id") or e.get("run_id")
        for e in raw_events
        if e.get("event_type") == "audit_flush"
    }
    for run_id in audit_run_ids:
        if run_id:
            # Fire-and-forget background verification
            import asyncio
            asyncio.create_task(_verify_run_background(run_id, org.id))

    return IngestResponse(accepted=accepted)


async def _process_event(raw: dict, org_id: str, db: AsyncSession) -> None:
    """Store a single event and handle type-specific processing."""
    event_type = raw.get("event_type", "")
    run_id = raw.get("run_id", "")
    occurred_at_str = raw.get("occurred_at")
    occurred_at = (
        datetime.fromisoformat(occurred_at_str.replace("Z", "+00:00"))
        if occurred_at_str
        else datetime.utcnow()
    )

    # 1. Write to raw log (idempotent on event_id)
    event_id = raw.get("event_id", "")
    existing = await db.execute(
        select(CloudEventLog).where(CloudEventLog.event_id == event_id)
    )
    if existing.scalar_one_or_none() is None:
        db.add(CloudEventLog(
            event_id=event_id,
            org_id=org_id,
            run_id=run_id,
            agent_name=raw.get("agent_name", ""),
            project=raw.get("project", "default"),
            event_type=event_type,
            payload=raw.get("payload", {}),
            occurred_at=occurred_at,
        ))

    # 2. Type-specific handling
    if event_type == "audit_flush":
        await _handle_audit_flush(raw, org_id, db)
    elif event_type == "run_start":
        await _handle_run_start(raw, org_id, db)
    elif event_type == "run_complete":
        await _handle_run_complete(raw, org_id, occurred_at, db)
    elif event_type == "turn_complete":
        await _handle_turn_complete(raw, org_id, db)
    elif event_type == "run_error":
        await _handle_run_error(raw, org_id, occurred_at, db)
    elif event_type == "circuit_state_change":
        await _handle_circuit_state_change(raw, org_id, occurred_at, db)


async def _handle_run_start(raw: dict, org_id: str, db: AsyncSession) -> None:
    run_id = raw.get("run_id", "")
    project = raw.get("project", "default")
    agent_name = raw.get("agent_name", "")
    payload = raw.get("payload", {})
    occurred_at_str = raw.get("occurred_at")
    started_at = (
        datetime.fromisoformat(occurred_at_str.replace("Z", "+00:00"))
        if occurred_at_str
        else datetime.utcnow()
    )

    # Create a placeholder AuditRun if audit_flush hasn't arrived yet.
    existing_run = await db.execute(select(AuditRun).where(AuditRun.run_id == run_id))
    if existing_run.scalar_one_or_none() is None:
        db.add(AuditRun(
            org_id=org_id,
            project=project,
            agent_name=agent_name,
            run_id=run_id,
            final_root_hash="",
            event_count=0,
            started_at=started_at,
            integrity="pending",
        ))

    # Populate ActiveRunCache for fleet dashboard
    existing_cache = await db.execute(
        select(ActiveRunCache).where(ActiveRunCache.run_id == run_id)
    )
    if existing_cache.scalar_one_or_none() is None:
        db.add(ActiveRunCache(
            run_id=run_id,
            org_id=org_id,
            project=project,
            agent_name=agent_name,
            model=payload.get("model") or "",
            started_at=started_at,
            prompt_hash=payload.get("prompt_hash") or "",
        ))


async def _handle_run_complete(
    raw: dict, org_id: str, occurred_at: datetime, db: AsyncSession
) -> None:
    run_id = raw.get("run_id", "")
    result = await db.execute(select(AuditRun).where(AuditRun.run_id == run_id))
    run = result.scalar_one_or_none()
    if run and run.completed_at is None:
        run.completed_at = occurred_at

    payload = raw.get("payload", {})
    total_turns = payload.get("total_turns", 0) or 0
    await _upsert_metric_snapshot(run_id, org_id, occurred_at, total_turns, success=True, db=db)


async def _handle_turn_complete(raw: dict, org_id: str, db: AsyncSession) -> None:
    run_id = raw.get("run_id", "")
    payload = raw.get("payload", {})
    result = await db.execute(
        select(ActiveRunCache).where(ActiveRunCache.run_id == run_id)
    )
    cache = result.scalar_one_or_none()
    if cache is not None:
        cache.turns_so_far += 1
        cache.input_tokens += int(payload.get("input_tokens") or 0)
        cache.output_tokens += int(payload.get("output_tokens") or 0)
        cache.cost_so_far_usd += float(payload.get("cost_usd") or 0.0)


async def _handle_run_error(
    raw: dict, org_id: str, occurred_at: datetime, db: AsyncSession
) -> None:
    run_id = raw.get("run_id", "")
    payload = raw.get("payload", {})
    total_turns = payload.get("turn_count", 0) or 0
    await _upsert_metric_snapshot(run_id, org_id, occurred_at, total_turns, success=False, db=db)


async def _handle_circuit_state_change(
    raw: dict, org_id: str, occurred_at: datetime, db: AsyncSession
) -> None:
    payload = raw.get("payload", {})
    agent_name = raw.get("agent_name", "")
    project = raw.get("project", "default")
    resource = payload.get("resource", "")
    new_state = payload.get("new_state", "")

    db.add(CircuitBreakerEvent(
        org_id=org_id,
        project=project,
        agent_name=agent_name,
        resource=resource,
        prev_state=payload.get("prev_state", ""),
        new_state=new_state,
        failure_count=int(payload.get("failure_count") or 0),
        occurred_at=occurred_at,
    ))

    # Trigger alert evaluation
    try:
        from app.alerting.evaluator import fire_circuit_breaker_open, resolve_circuit_breaker
        if new_state == "open":
            await fire_circuit_breaker_open(
                org_id=org_id,
                agent_name=agent_name,
                project=project,
                resource=resource,
                failure_count=int(payload.get("failure_count") or 0),
                occurred_at=occurred_at,
                db=db,
            )
        elif new_state == "closed":
            await resolve_circuit_breaker(
                org_id=org_id,
                agent_name=agent_name,
                resource=resource,
                db=db,
            )
    except Exception as exc:
        import logging
        logging.getLogger("agentkit.cloud.ingest").debug(
            "Alert trigger failed for circuit_state_change: %s", exc
        )


async def _upsert_metric_snapshot(
    run_id: str,
    org_id: str,
    occurred_at: datetime,
    total_turns: int,
    success: bool,
    db: AsyncSession,
) -> None:
    """Aggregate run metrics into the minute bucket and clean up ActiveRunCache."""
    # Read accumulated data from ActiveRunCache
    cache_result = await db.execute(
        select(ActiveRunCache).where(ActiveRunCache.run_id == run_id)
    )
    cache = cache_result.scalar_one_or_none()

    project = cache.project if cache else "default"
    agent_name = cache.agent_name if cache else ""
    model = cache.model if cache else ""
    input_tokens = cache.input_tokens if cache else 0
    output_tokens = cache.output_tokens if cache else 0
    cost_usd = cache.cost_so_far_usd if cache else 0.0
    started_at = cache.started_at if cache else occurred_at
    duration_ms = int((occurred_at - started_at).total_seconds() * 1000)

    # Bucket = current minute
    bucket = occurred_at.replace(second=0, microsecond=0)

    # Upsert AgentMetricSnapshot
    snap_result = await db.execute(
        select(AgentMetricSnapshot).where(
            AgentMetricSnapshot.org_id == org_id,
            AgentMetricSnapshot.project == project,
            AgentMetricSnapshot.agent_name == agent_name,
            AgentMetricSnapshot.model == model,
            AgentMetricSnapshot.bucket == bucket,
        )
    )
    snap = snap_result.scalar_one_or_none()
    if snap is None:
        db.add(AgentMetricSnapshot(
            org_id=org_id,
            project=project,
            agent_name=agent_name,
            model=model,
            bucket=bucket,
            runs_total=1,
            runs_success=1 if success else 0,
            runs_error=0 if success else 1,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            total_turns=total_turns,
            total_duration_ms=duration_ms,
        ))
    else:
        snap.runs_total += 1
        if success:
            snap.runs_success += 1
        else:
            snap.runs_error += 1
        snap.input_tokens += input_tokens
        snap.output_tokens += output_tokens
        snap.cost_usd += cost_usd
        snap.total_turns += total_turns
        snap.total_duration_ms += duration_ms

    # Remove from active cache
    if cache is not None:
        await db.delete(cache)


async def _handle_audit_flush(raw: dict, org_id: str, db: AsyncSession) -> None:
    """Upsert AuditRun and insert AuditEvents from an audit_flush payload."""
    run_id = raw.get("run_id", "")
    payload = raw.get("payload", {})
    agent_name = raw.get("agent_name", "")
    project = raw.get("project", "default")
    final_root_hash = payload.get("final_root_hash", "")
    raw_events: list[dict] = payload.get("events", [])

    # Upsert AuditRun
    result = await db.execute(select(AuditRun).where(AuditRun.run_id == run_id))
    run = result.scalar_one_or_none()
    if run is None:
        run = AuditRun(
            org_id=org_id,
            project=project,
            agent_name=agent_name,
            run_id=run_id,
            final_root_hash=final_root_hash,
            event_count=len(raw_events),
            integrity="pending",
        )
        db.add(run)
    else:
        run.final_root_hash = final_root_hash
        run.event_count = len(raw_events)
        run.agent_name = agent_name or run.agent_name
        run.project = project or run.project

    # Insert AuditEvents (skip on conflict — idempotent delivery)
    for idx, ev in enumerate(raw_events):
        existing = await db.execute(
            select(AuditEvent).where(AuditEvent.event_id == ev.get("event_id", ""))
        )
        if existing.scalar_one_or_none() is not None:
            continue

        ts_str = ev.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            ts = datetime.utcnow()

        db.add(AuditEvent(
            run_id=run_id,
            org_id=org_id,
            event_id=ev.get("event_id", ""),
            event_type=ev.get("event_type", ""),
            actor=ev.get("actor", ""),
            payload_hash=ev.get("payload_hash", ""),
            prev_root=ev.get("prev_root", ""),
            leaf_hash=ev.get("leaf_hash", ""),
            seq=idx,
            timestamp=ts,
            verified=False,
        ))


async def _verify_run_background(run_id: str, org_id: str) -> None:
    """
    Background task: re-verify Merkle chain for a run and update integrity status.
    Runs after the ingest commit so all events are visible.
    """
    import logging
    _log = logging.getLogger("agentkit.cloud.ingest")
    try:
        from app.database import SessionLocal
    except Exception as exc:
        _log.debug("verify_run_background: could not import SessionLocal: %s", exc)
        return
    try:
      async with SessionLocal() as db:
            result = await db.execute(select(AuditRun).where(AuditRun.run_id == run_id))
            run = result.scalar_one_or_none()
            if run is None:
                return

            events_result = await db.execute(
                select(AuditEvent)
                .where(AuditEvent.run_id == run_id)
                .order_by(AuditEvent.seq)
            )
            events = list(events_result.scalars().all())

            ok, broken_seq, expected, stored = verify_chain(events)
            run.integrity = "verified" if ok else "failed"

            if ok:
                for event in events:
                    event.verified = True
            else:
                # Trigger audit_integrity_failure alert
                try:
                    from app.alerting.evaluator import fire_audit_integrity_failure
                    await fire_audit_integrity_failure(
                        org_id=org_id,
                        agent_name=run.agent_name,
                        project=run.project,
                        run_id=run_id,
                        db=db,
                    )
                except Exception:
                    pass

            await db.commit()
    except Exception as exc:
        _log.debug("verify_run_background failed for run %s: %s", run_id, exc)
