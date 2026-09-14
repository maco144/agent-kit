"""
Evidence bundles: a signed zip of audit chains for a period.

manifest.json pins every other file by SHA-256 and manifest.sig signs the exact
manifest bytes, so a verifier holding agent-kit's public key can prove nothing in
the bundle was altered. Chains are re-verified at export time (verification.json);
the verifier re-derives them independently from events.jsonl.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit_chain import verify_chain
from app.compliance import signing
from app.compliance.retention import effective_retention, receipt_fields
from app.models import AuditEvent, AuditRun, DeletionReceipt, LegalHold, Organization

FORMAT = "agentkit-evidence-bundle/1"
MAX_RUNS = 10_000
_CHUNK = 500


class BundleTooLarge(ValueError):
    """The requested scope contains more runs than one bundle may hold."""


async def build_bundle(
    org: Organization,
    db: AsyncSession,
    from_: datetime,
    to: datetime,
    project: str | None = None,
    agent_name: str | None = None,
) -> bytes:
    start = func.coalesce(AuditRun.started_at, AuditRun.created_at)
    scope = [AuditRun.org_id == org.id, start >= from_, start < to]
    if project:
        scope.append(AuditRun.project == project)
    if agent_name:
        scope.append(AuditRun.agent_name == agent_name)

    count = (await db.execute(select(func.count()).select_from(AuditRun).where(*scope))).scalar_one()
    if count > MAX_RUNS:
        raise BundleTooLarge(f"{count} runs in scope; a bundle holds at most {MAX_RUNS}. Narrow the time range.")
    runs = (await db.execute(select(AuditRun).where(*scope).order_by(start, AuditRun.run_id))).scalars().all()

    events_by_run: dict[str, list[AuditEvent]] = {run.run_id: [] for run in runs}
    run_ids = list(events_by_run)
    for i in range(0, len(run_ids), _CHUNK):
        rows = (
            await db.execute(select(AuditEvent).where(AuditEvent.run_id.in_(run_ids[i:i + _CHUNK])).order_by(AuditEvent.seq))
        ).scalars().all()
        for event in rows:
            events_by_run[event.run_id].append(event)

    verification_runs = []
    for run in runs:
        ok, broken_seq, _, _ = verify_chain(events_by_run[run.run_id])
        verification_runs.append({"run_id": run.run_id, "verified": ok, "broken_seq": broken_seq})

    receipt_q = select(DeletionReceipt).where(
        DeletionReceipt.org_id == org.id, DeletionReceipt.deleted_at >= from_, DeletionReceipt.deleted_at < to
    )
    if project:
        receipt_q = receipt_q.where(DeletionReceipt.project == project)
    if agent_name:
        receipt_q = receipt_q.where(DeletionReceipt.agent_name == agent_name)
    receipts = (await db.execute(receipt_q.order_by(DeletionReceipt.deleted_at))).scalars().all()

    holds = (
        await db.execute(
            select(LegalHold).where(LegalHold.org_id == org.id, LegalHold.released_at.is_(None)).order_by(LegalHold.created_at)
        )
    ).scalars().all()

    data_files = {
        "runs.jsonl": _jsonl(
            {
                "run_id": r.run_id,
                "project": r.project,
                "agent_name": r.agent_name,
                "chain_origin": r.chain_origin,
                "started_at": _iso(r.started_at),
                "completed_at": _iso(r.completed_at),
                "event_count": r.event_count,
                "final_root_hash": r.final_root_hash,
                "integrity": r.integrity,
            }
            for r in runs
        ),
        "events.jsonl": _jsonl(
            {
                "run_id": e.run_id,
                "seq": e.seq,
                "event_id": e.event_id,
                "event_type": e.event_type,
                "actor": e.actor,
                "payload_hash": e.payload_hash,
                "prev_root": e.prev_root,
                "leaf_hash": e.leaf_hash,
                "timestamp": e.timestamp.isoformat(),
            }
            for r in runs
            for e in events_by_run[r.run_id]
        ),
        "verification.json": json.dumps(
            {
                "runs": verification_runs,
                "verified": sum(1 for v in verification_runs if v["verified"]),
                "failed": sum(1 for v in verification_runs if not v["verified"]),
            },
            indent=2,
        ).encode(),
        "deletions.jsonl": _jsonl({**receipt_fields(r), "kid": r.kid, "signature": r.signature} for r in receipts),
    }

    days, source = effective_retention(org)
    kid, _ = await signing.active_key(db)
    manifest = {
        "format": FORMAT,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "org": {"id": org.id, "name": org.name, "tier": org.tier},
        "scope": {"from": from_.isoformat(), "to": to.isoformat(), "project": project, "agent_name": agent_name},
        "retention": {"audit_retention_days": days, "source": source},
        "legal_holds": [
            {"id": h.id, "project": h.project, "run_id": h.run_id, "reason": h.reason, "created_at": h.created_at.isoformat()}
            for h in holds
        ],
        "counts": {
            "runs": len(runs),
            "events": sum(len(v) for v in events_by_run.values()),
            "deletions": len(receipts),
        },
        "files": {name: hashlib.sha256(content).hexdigest() for name, content in data_files.items()},
        "signing": {"kid": kid, "alg": signing.ALG, "keys_url": signing.KEYS_PATH},
    }
    manifest_bytes = json.dumps(manifest, indent=2).encode()
    signed_kid, signature = await signing.sign(db, manifest_bytes)
    signature_bytes = json.dumps({"kid": signed_kid, "alg": signing.ALG, "signature": signature}).encode()

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", manifest_bytes)
        zf.writestr("manifest.sig", signature_bytes)
        for name, content in data_files.items():
            zf.writestr(name, content)
    return buffer.getvalue()


def _jsonl(records: Iterable[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(record) + "\n" for record in records).encode()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
