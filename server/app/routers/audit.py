"""GET /v1/audit/* — audit trail search, retrieval, verification, and export."""

from __future__ import annotations

import base64
import csv
import io
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit_chain import verify_chain
from app.auth import get_current_org
from app.database import get_db
from app.models import AuditEvent, AuditRun, Organization
from app.schemas import (
    AuditEventList,
    AuditEventSchema,
    AuditRunDetail,
    AuditRunList,
    AuditRunSummary,
    VerifyFailure,
    VerifySuccess,
)

router = APIRouter(prefix="/v1/audit", tags=["audit"])

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 500


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@router.get("/runs", response_model=AuditRunList)
async def list_runs(
    project: str | None = Query(None),
    agent_name: str | None = Query(None),
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    integrity: str | None = Query(None),
    limit: int = Query(_DEFAULT_LIMIT, le=_MAX_LIMIT, ge=1),
    cursor: str | None = Query(None),
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> AuditRunList:
    q = select(AuditRun).where(AuditRun.org_id == org.id)

    if project:
        q = q.where(AuditRun.project == project)
    if agent_name:
        if agent_name.endswith("*"):
            q = q.where(AuditRun.agent_name.like(agent_name[:-1] + "%"))
        else:
            q = q.where(AuditRun.agent_name == agent_name)
    if from_:
        q = q.where(AuditRun.created_at >= from_)
    if to:
        q = q.where(AuditRun.created_at <= to)
    if integrity:
        q = q.where(AuditRun.integrity == integrity)
    if cursor:
        try:
            cursor_val = base64.b64decode(cursor).decode()
            q = q.where(AuditRun.created_at < datetime.fromisoformat(cursor_val))
        except Exception:
            pass

    # Count total (before pagination)
    count_result = await db.execute(select(func.count()).select_from(q.subquery()))
    total = count_result.scalar_one()

    q = q.order_by(AuditRun.created_at.desc()).limit(limit + 1)
    result = await db.execute(q)
    rows = list(result.scalars().all())

    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = base64.b64encode(last.created_at.isoformat().encode()).decode()

    return AuditRunList(
        runs=[AuditRunSummary.model_validate(r) for r in rows],
        next_cursor=next_cursor,
        total=total,
    )


@router.get("/runs/{run_id}", response_model=AuditRunDetail)
async def get_run(
    run_id: str,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> AuditRunDetail:
    run = await _get_run_or_404(run_id, org.id, db)
    events_result = await db.execute(
        select(AuditEvent).where(AuditEvent.run_id == run_id).order_by(AuditEvent.seq)
    )
    events = list(events_result.scalars().all())
    # Build from AuditRunSummary first to avoid triggering lazy-load of run.events
    summary = AuditRunSummary.model_validate(run)
    return AuditRunDetail(
        **summary.model_dump(),
        events=[AuditEventSchema.model_validate(e) for e in events],
    )


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


@router.get("/runs/{run_id}/verify")
async def verify_run(
    run_id: str,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> VerifySuccess | VerifyFailure:
    """Re-derive all hashes on demand. Does not mutate stored integrity status."""
    await _get_run_or_404(run_id, org.id, db)

    events_result = await db.execute(
        select(AuditEvent).where(AuditEvent.run_id == run_id).order_by(AuditEvent.seq)
    )
    events = list(events_result.scalars().all())
    now = datetime.utcnow()

    ok, broken_seq, expected, stored = verify_chain(events)
    if ok:
        return VerifySuccess(
            run_id=run_id,
            verified=True,
            event_count=len(events),
            final_root_hash=events[-1].leaf_hash if events else "0" * 64,
            verified_at=now,
        )
    return VerifyFailure(
        run_id=run_id,
        verified=False,
        broken_at_seq=broken_seq or 0,
        broken_at_event_id=next(
            (e.event_id for e in events if e.seq == broken_seq), ""
        ),
        expected_leaf_hash=expected or "",
        stored_leaf_hash=stored or "",
        verified_at=now,
    )


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


@router.get("/runs/{run_id}/export")
async def export_run(
    run_id: str,
    format: str = Query("jsonl", pattern="^(jsonl|csv)$"),
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Export a run's audit chain.

    - jsonl: one JSON object per line, compatible with AuditChain.export_jsonl()
    - csv: flat tabular format
    """
    await _get_run_or_404(run_id, org.id, db)

    events_result = await db.execute(
        select(AuditEvent).where(AuditEvent.run_id == run_id).order_by(AuditEvent.seq)
    )
    events = list(events_result.scalars().all())

    if format == "jsonl":
        lines = [
            json.dumps({
                "event_id": e.event_id,
                "event_type": e.event_type,
                "actor": e.actor,
                "payload_hash": e.payload_hash,
                "prev_root": e.prev_root,
                "leaf_hash": e.leaf_hash,
                "timestamp": e.timestamp.isoformat(),
            })
            for e in events
        ]
        return Response(
            content="\n".join(lines),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f'attachment; filename="audit_{run_id}.jsonl"'},
        )

    # CSV
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["seq", "event_id", "event_type", "actor",
                    "payload_hash", "prev_root", "leaf_hash", "timestamp", "verified"],
    )
    writer.writeheader()
    for e in events:
        writer.writerow({
            "seq": e.seq,
            "event_id": e.event_id,
            "event_type": e.event_type,
            "actor": e.actor,
            "payload_hash": e.payload_hash,
            "prev_root": e.prev_root,
            "leaf_hash": e.leaf_hash,
            "timestamp": e.timestamp.isoformat(),
            "verified": e.verified,
        })
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="audit_{run_id}.csv"'},
    )


# ---------------------------------------------------------------------------
# Event search
# ---------------------------------------------------------------------------


@router.get("/events", response_model=AuditEventList)
async def list_events(
    event_type: str | None = Query(None),
    actor: str | None = Query(None),
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    project: str | None = Query(None),
    limit: int = Query(_DEFAULT_LIMIT, le=_MAX_LIMIT, ge=1),
    cursor: str | None = Query(None),
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> AuditEventList:
    q = select(AuditEvent).where(AuditEvent.org_id == org.id)

    if event_type:
        q = q.where(AuditEvent.event_type == event_type)
    if actor:
        q = q.where(AuditEvent.actor == actor)
    if from_:
        q = q.where(AuditEvent.timestamp >= from_)
    if to:
        q = q.where(AuditEvent.timestamp <= to)
    if project:
        # Join to AuditRun for project filter
        q = q.join(AuditRun, AuditRun.run_id == AuditEvent.run_id).where(
            AuditRun.project == project
        )
    if cursor:
        try:
            cursor_val = base64.b64decode(cursor).decode()
            q = q.where(AuditEvent.timestamp < datetime.fromisoformat(cursor_val))
        except Exception:
            pass

    count_result = await db.execute(select(func.count()).select_from(q.subquery()))
    total = count_result.scalar_one()

    q = q.order_by(AuditEvent.timestamp.desc()).limit(limit + 1)
    result = await db.execute(q)
    rows = list(result.scalars().all())

    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = base64.b64encode(last.timestamp.isoformat().encode()).decode()

    return AuditEventList(
        events=[AuditEventSchema.model_validate(e) for e in rows],
        next_cursor=next_cursor,
        total=total,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_run_or_404(run_id: str, org_id: str, db: AsyncSession) -> AuditRun:
    result = await db.execute(
        select(AuditRun).where(AuditRun.run_id == run_id, AuditRun.org_id == org_id)
    )
    run = result.scalar_one_or_none()
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Audit run '{run_id}' not found.",
        )
    return run
