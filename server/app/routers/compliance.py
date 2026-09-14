"""Compliance exports: signing keys, evidence bundles, retention, legal holds, deletion receipts."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_org
from app.compliance import signing
from app.compliance.retention import MAX_RETENTION_DAYS, effective_retention
from app.database import get_db
from app.models import AuditRun, DeletionReceipt, LegalHold, Organization
from app.schemas import (
    CreateHoldRequest,
    DeletionList,
    DeletionReceiptOut,
    HoldList,
    HoldOut,
    RetentionOut,
    UpdateRetentionRequest,
)

wellknown_router = APIRouter(tags=["compliance"])
router = APIRouter(prefix="/v1/compliance", tags=["compliance"])


@wellknown_router.get(signing.KEYS_PATH)
async def signing_keys(db: AsyncSession = Depends(get_db)) -> dict[str, list[dict[str, object]]]:
    """Public keys for verifying evidence bundles and deletion receipts. No authentication."""
    await signing.active_key(db)  # ensure a key exists before anyone needs to verify
    await db.commit()
    return {"keys": await signing.public_keys(db)}


def _retention(org: Organization) -> RetentionOut:
    days, source = effective_retention(org)
    return RetentionOut(tier=org.tier, audit_retention_days=days, source=source, configurable=org.tier == "enterprise")


@router.get("/retention", response_model=RetentionOut)
async def get_retention(org: Organization = Depends(get_current_org)) -> RetentionOut:
    return _retention(org)


@router.put("/retention", response_model=RetentionOut)
async def put_retention(
    body: UpdateRetentionRequest,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> RetentionOut:
    if org.tier != "enterprise":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Audit retention is configurable on the enterprise tier.")
    days = body.audit_retention_days
    if days is not None and not 1 <= days <= MAX_RETENTION_DAYS:
        raise HTTPException(status_code=400, detail=f"audit_retention_days must be between 1 and {MAX_RETENTION_DAYS}")
    org.audit_retention_days = days
    await db.commit()
    return _retention(org)


@router.get("/holds", response_model=HoldList)
async def list_holds(org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db)) -> HoldList:
    rows = (await db.execute(select(LegalHold).where(LegalHold.org_id == org.id).order_by(LegalHold.created_at))).scalars().all()
    return HoldList(holds=[HoldOut.model_validate(h) for h in rows])


@router.post("/holds", response_model=HoldOut, status_code=status.HTTP_201_CREATED)
async def create_hold(
    body: CreateHoldRequest,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> HoldOut:
    if bool(body.project) == bool(body.run_id):
        raise HTTPException(status_code=400, detail="Set exactly one of project or run_id.")
    if body.run_id:
        run = (await db.execute(select(AuditRun).where(AuditRun.run_id == body.run_id, AuditRun.org_id == org.id))).scalar_one_or_none()
        if run is None:
            raise HTTPException(status_code=404, detail="Audit run not found")
    hold = LegalHold(org_id=org.id, project=body.project, run_id=body.run_id, reason=body.reason)
    db.add(hold)
    await db.commit()
    await db.refresh(hold)
    return HoldOut.model_validate(hold)


@router.post("/holds/{hold_id}/release", response_model=HoldOut)
async def release_hold(
    hold_id: str,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> HoldOut:
    hold = (await db.execute(select(LegalHold).where(LegalHold.id == hold_id, LegalHold.org_id == org.id))).scalar_one_or_none()
    if hold is None:
        raise HTTPException(status_code=404, detail="Legal hold not found")
    hold.released_at = hold.released_at or datetime.utcnow()
    await db.commit()
    return HoldOut.model_validate(hold)


@router.get("/deletions", response_model=DeletionList)
async def list_deletions(
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> DeletionList:
    q = select(DeletionReceipt).where(DeletionReceipt.org_id == org.id)
    if from_:
        q = q.where(DeletionReceipt.deleted_at >= from_)
    if to:
        q = q.where(DeletionReceipt.deleted_at < to)
    rows = (await db.execute(q.order_by(DeletionReceipt.deleted_at))).scalars().all()
    return DeletionList(deletions=[DeletionReceiptOut.model_validate(r) for r in rows])
