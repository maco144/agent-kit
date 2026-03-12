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
from app.models import AuditEvent, AuditRun, CloudEventLog, Organization
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


async def _handle_run_start(raw: dict, org_id: str, db: AsyncSession) -> None:
    run_id = raw.get("run_id", "")
    project = raw.get("project", "default")
    agent_name = raw.get("agent_name", "")
    occurred_at_str = raw.get("occurred_at")
    started_at = (
        datetime.fromisoformat(occurred_at_str.replace("Z", "+00:00"))
        if occurred_at_str
        else datetime.utcnow()
    )
    # Create a placeholder AuditRun if audit_flush hasn't arrived yet.
    # audit_flush will fill in the events and final_root_hash.
    existing = await db.execute(
        select(AuditRun).where(AuditRun.run_id == run_id)
    )
    if existing.scalar_one_or_none() is None:
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


async def _handle_run_complete(
    raw: dict, org_id: str, occurred_at: datetime, db: AsyncSession
) -> None:
    run_id = raw.get("run_id", "")
    result = await db.execute(select(AuditRun).where(AuditRun.run_id == run_id))
    run = result.scalar_one_or_none()
    if run and run.completed_at is None:
        run.completed_at = occurred_at


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

            await db.commit()
    except Exception as exc:
        _log.debug("verify_run_background failed for run %s: %s", run_id, exc)
