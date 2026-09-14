"""
Audit retention: tier policy, enterprise override, legal holds, and purge with signed receipts.

Only audit runs and their events are purged; metrics retention is separate. Every
deleted run leaves a DeletionReceipt signed with the active signing key, written in
the same transaction as the deletion.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.compliance import signing
from app.models import AuditEvent, AuditRun, DeletionReceipt, LegalHold, Organization

logger = logging.getLogger("agentkit.cloud.compliance")

TIER_RETENTION_DAYS = {"free": 7, "pro": 90, "enterprise": 365}
MAX_RETENTION_DAYS = 2555  # 7 years
RECEIPT_FIELDS = (
    "run_id", "org_id", "project", "agent_name", "final_root_hash", "event_count",
    "chain_origin", "started_at", "completed_at", "deleted_at", "reason",
)


def effective_retention(org: Organization) -> tuple[int, str]:
    if org.tier == "enterprise" and org.audit_retention_days:
        return org.audit_retention_days, "override"
    return TIER_RETENTION_DAYS.get(org.tier, TIER_RETENTION_DAYS["free"]), "tier"


def receipt_fields(receipt: DeletionReceipt) -> dict[str, Any]:
    """The exact fields a receipt's signature covers."""
    values: dict[str, Any] = {}
    for name in RECEIPT_FIELDS:
        value = getattr(receipt, name)
        values[name] = value.isoformat() if isinstance(value, datetime) else value
    return values


async def purge_expired(db: AsyncSession, now: datetime | None = None, batch_size: int = 500) -> int:
    """Delete expired, unheld audit runs org by org; one transaction per org batch."""
    current = now or datetime.utcnow()
    total = 0
    for org in (await db.execute(select(Organization))).scalars().all():
        try:
            total += await _purge_org(org, db, current, batch_size)
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("Audit purge failed for org %s", org.id)
    return total


async def _purge_org(org: Organization, db: AsyncSession, now: datetime, batch_size: int) -> int:
    days, _ = effective_retention(org)
    holds = (
        await db.execute(select(LegalHold).where(LegalHold.org_id == org.id, LegalHold.released_at.is_(None)))
    ).scalars().all()
    held_projects = {h.project for h in holds if h.project}
    held_runs = {h.run_id for h in holds if h.run_id}

    q = (
        select(AuditRun)
        .where(
            AuditRun.org_id == org.id,
            func.coalesce(AuditRun.completed_at, AuditRun.created_at) < now - timedelta(days=days),
        )
        .order_by(AuditRun.created_at)
        .limit(batch_size)
    )
    if held_projects:
        q = q.where(AuditRun.project.not_in(held_projects))
    if held_runs:
        q = q.where(AuditRun.run_id.not_in(held_runs))
    runs = (await db.execute(q)).scalars().all()

    for run in runs:
        receipt = DeletionReceipt(
            org_id=org.id,
            run_id=run.run_id,
            project=run.project,
            agent_name=run.agent_name,
            final_root_hash=run.final_root_hash,
            event_count=run.event_count,
            chain_origin=run.chain_origin,
            started_at=run.started_at,
            completed_at=run.completed_at,
            deleted_at=now,
            reason="retention",
            kid="",
            signature="",
        )
        receipt.kid, receipt.signature = await signing.sign(db, signing.canonical_bytes(receipt_fields(receipt)))
        db.add(receipt)
        await db.execute(delete(AuditEvent).where(AuditEvent.run_id == run.run_id))
        await db.delete(run)
    return len(runs)
