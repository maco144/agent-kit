# Compliance Exports Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Signed, offline-verifiable evidence bundles of audit chains; audit retention with legal holds and signed deletion receipts; an `agent-kit verify` CLI.

**Architecture:** `app/compliance/signing.py` manages Ed25519 keys (env-supplied or generated and persisted) and publishes public keys. `retention.py` computes effective retention, enforces holds, and purges expired runs with signed receipts. `bundle.py` assembles a zip whose `manifest.json` pins every file by SHA-256 and is signed byte-for-byte. The SDK's `agent_kit/compliance.py` re-verifies everything offline; `agent_kit/cli.py` wraps it.

**Tech Stack:** FastAPI, SQLAlchemy 2 async, Alembic, `cryptography>=41` (Ed25519), stdlib `zipfile`; SDK httpx + argparse; pytest + pytest-asyncio (auto).

**Spec:** `specs/10-compliance-exports.md`

## Global Constraints

- Server never imports the SDK; the SDK verifier reimplements nothing server-specific beyond the bundle format and receipt canonical bytes (defined identically in both, tested against each other end to end).
- Signature over exact `manifest.json` bytes; receipts over `json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()` for the spec's receipt field list, datetimes `isoformat()` or `null`.
- Every export / purge / holds query is org-scoped.
- A purge batch either writes receipts and deletes, or does nothing.
- Wording: "supports record-keeping obligations"; never "compliant" or "certified".
- Server: `ruff check app tests`, `pytest`, `alembic upgrade head` clean. SDK: `ruff check agent_kit tests`, `mypy agent_kit`, `pytest` clean with and without `cryptography` (SDK compliance tests skip without it).

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `server/pyproject.toml` | `cryptography>=41` | Modify |
| `server/migrations/versions/007_compliance.py` | `signing_keys`, `legal_holds`, `deletion_receipts`, `organizations.audit_retention_days` | Create |
| `server/app/models.py` | `SigningKey`, `LegalHold`, `DeletionReceipt`, `Organization.audit_retention_days` | Modify |
| `server/app/compliance/__init__.py` | Package docstring | Create |
| `server/app/compliance/signing.py` | Keys, signing, canonical receipt bytes | Create |
| `server/app/compliance/retention.py` | Effective retention, holds, purge, receipts | Create |
| `server/app/compliance/bundle.py` | Evidence bundle builder | Create |
| `server/app/routers/compliance.py` | Keys endpoint + `/v1/compliance/*` | Create |
| `server/app/main.py` | Routers + worker purge | Modify |
| `server/tests/test_compliance.py` | Server behaviour | Create |
| `agent_kit/compliance.py` | `BundleReport`, `load_public_keys`, `verify_bundle` | Create |
| `agent_kit/cli.py` | `agent-kit verify` | Create |
| `pyproject.toml`, `.github/workflows/ci.yml` | `compliance` extra, console script, CI install | Modify |
| `tests/test_compliance.py` | Verifier + CLI | Create |
| `docs/api-reference.md`, `docs/cloud-quickstart.md`, `docs/self-hosting.md`, `README.md`, `CHANGELOG.md`, `specs/06-harness-roadmap.md`, `specs/10-compliance-exports.md`, `PROJECT_INDEX.md` | Docs | Modify |

---

### Task 1: Schema and signing keys

**Files:**
- Create: `server/migrations/versions/007_compliance.py`, `server/app/compliance/__init__.py`, `server/app/compliance/signing.py`, `server/app/routers/compliance.py`, `server/tests/test_compliance.py`
- Modify: `server/pyproject.toml`, `server/app/models.py`, `server/app/main.py`

**Interfaces:**
- Produces: models `SigningKey`, `LegalHold`, `DeletionReceipt`, `Organization.audit_retention_days`; `signing.ALG`, `signing.active_key(db) -> tuple[str, Ed25519PrivateKey]`, `async signing.sign(db, data: bytes) -> tuple[str, str]`, `async signing.public_keys(db) -> list[dict]`, `signing.canonical_bytes(fields: dict) -> bytes`, `signing.kid_for(public_key: bytes) -> str`; route `GET /.well-known/agentkit-signing-keys`; `routers.compliance.wellknown_router`, `routers.compliance.router` (prefix `/v1/compliance`).

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_compliance.py
"""Compliance exports: signing keys, retention, holds, purge receipts, evidence bundles."""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from httpx import ASGITransport, AsyncClient

from app.compliance import signing
from app.models import SigningKey


def verify_sig(public_key_b64: str, signature_b64: str, data: bytes) -> None:
    Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64)).verify(base64.b64decode(signature_b64), data)


async def keys_by_kid(db) -> dict[str, SigningKey]:
    from sqlalchemy import select

    return {k.kid: k for k in (await db.execute(select(SigningKey))).scalars().all()}


async def test_generated_key_is_persisted_and_reused(db, monkeypatch):
    monkeypatch.delenv("AGENTKIT_SIGNING_KEY", raising=False)
    kid1, sig1 = await signing.sign(db, b"hello")
    await db.commit()
    kid2, _ = await signing.sign(db, b"again")
    await db.commit()

    assert kid1 == kid2
    row = (await keys_by_kid(db))[kid1]
    assert (row.source, row.active, row.private_key is not None) == ("generated", True, True)
    verify_sig(row.public_key, sig1, b"hello")


async def test_env_key_takes_over_and_old_key_stays_published(db, monkeypatch):
    monkeypatch.delenv("AGENTKIT_SIGNING_KEY", raising=False)
    old_kid, old_sig = await signing.sign(db, b"old bundle")
    await db.commit()

    seed = Ed25519PrivateKey.generate().private_bytes_raw()
    monkeypatch.setenv("AGENTKIT_SIGNING_KEY", base64.b64encode(seed).decode())
    new_kid, new_sig = await signing.sign(db, b"new bundle")
    await db.commit()

    keys = await keys_by_kid(db)
    assert new_kid != old_kid
    assert (keys[new_kid].source, keys[new_kid].active, keys[new_kid].private_key) == ("env", True, None)
    assert keys[old_kid].active is False and keys[old_kid].retired_at is not None
    verify_sig(keys[old_kid].public_key, old_sig, b"old bundle")
    verify_sig(keys[new_kid].public_key, new_sig, b"new bundle")

    published = {k["kid"]: k for k in await signing.public_keys(db)}
    assert {old_kid, new_kid} <= set(published)
    assert published[new_kid]["active"] is True and published[old_kid]["active"] is False
    assert published[new_kid]["alg"] == "Ed25519"


async def test_env_key_id_override(db, monkeypatch):
    seed = Ed25519PrivateKey.generate().private_bytes_raw()
    monkeypatch.setenv("AGENTKIT_SIGNING_KEY", base64.b64encode(seed).decode())
    monkeypatch.setenv("AGENTKIT_SIGNING_KEY_ID", "customer-kms-2026")
    kid, _ = await signing.sign(db, b"x")
    await db.commit()
    assert kid == "customer-kms-2026"


def test_invalid_env_key_is_a_clear_error(monkeypatch):
    monkeypatch.setenv("AGENTKIT_SIGNING_KEY", "not-a-key")
    with pytest.raises(RuntimeError, match="AGENTKIT_SIGNING_KEY"):
        signing.load_env_key()


def test_canonical_bytes_are_sorted_and_compact():
    assert signing.canonical_bytes({"b": 1, "a": None}) == b'{"a":null,"b":1}'


async def test_keys_endpoint_needs_no_auth(db):
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get("/.well-known/agentkit-signing-keys")

    assert resp.status_code == 200
    keys = resp.json()["keys"]
    assert keys and all(k["alg"] == "Ed25519" and len(base64.b64decode(k["public_key"])) == 32 for k in keys)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && pip install -e ".[dev]" && pytest tests/test_compliance.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.compliance'`

- [ ] **Step 3: Implement**

`server/pyproject.toml` dependencies gain `"cryptography>=41",       # Ed25519 evidence signing`.

`server/app/models.py` — `Organization`, after `plan_metadata`:

```python
    audit_retention_days: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # enterprise override; None = tier default
```

After `Budget`:

```python
# ---------------------------------------------------------------------------
# Compliance: signing keys, legal holds, deletion receipts
# ---------------------------------------------------------------------------


class SigningKey(Base):
    """Ed25519 keys that sign evidence bundles and deletion receipts. Never deleted."""
    __tablename__ = "signing_keys"

    kid: Mapped[str] = mapped_column(String(64), primary_key=True)
    public_key: Mapped[str] = mapped_column(String(64), nullable=False)
    private_key: Mapped[str | None] = mapped_column(String(128), nullable=True)  # None for env keys
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # env | generated
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LegalHold(Base):
    """Blocks retention purges for a project or a single run until released."""
    __tablename__ = "legal_holds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False)
    project: Mapped[str | None] = mapped_column(String(255), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (Index("ix_legal_holds_org_released", "org_id", "released_at"),)


class DeletionReceipt(Base):
    """Signed proof that an audit run existed and was disposed of. Never purged."""
    __tablename__ = "deletion_receipts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    project: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(255), nullable=False)
    final_root_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    chain_origin: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    kid: Mapped[str] = mapped_column(String(64), nullable=False)
    signature: Mapped[str] = mapped_column(String(128), nullable=False)

    __table_args__ = (Index("ix_deletion_receipts_org_deleted", "org_id", "deleted_at"),)
```

```python
# server/migrations/versions/007_compliance.py
"""Compliance: signing keys, legal holds, deletion receipts, audit retention override.

Revision ID: 007
Revises: 006
Create Date: 2026-09-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("audit_retention_days", sa.Integer, nullable=True))
    op.create_table(
        "signing_keys",
        sa.Column("kid", sa.String(64), primary_key=True),
        sa.Column("public_key", sa.String(64), nullable=False),
        sa.Column("private_key", sa.String(128), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("active", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("retired_at", sa.DateTime, nullable=True),
    )
    op.create_table(
        "legal_holds",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False),
        sa.Column("project", sa.String(255), nullable=True),
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("released_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_legal_holds_org_released", "legal_holds", ["org_id", "released_at"])
    op.create_table(
        "deletion_receipts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("project", sa.String(255), nullable=False),
        sa.Column("agent_name", sa.String(255), nullable=False),
        sa.Column("final_root_hash", sa.String(64), nullable=False),
        sa.Column("event_count", sa.Integer, nullable=False),
        sa.Column("chain_origin", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime, nullable=True),
        sa.Column("completed_at", sa.DateTime, nullable=True),
        sa.Column("deleted_at", sa.DateTime, nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("kid", sa.String(64), nullable=False),
        sa.Column("signature", sa.String(128), nullable=False),
    )
    op.create_index("ix_deletion_receipts_org_deleted", "deletion_receipts", ["org_id", "deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_deletion_receipts_org_deleted", table_name="deletion_receipts")
    op.drop_table("deletion_receipts")
    op.drop_index("ix_legal_holds_org_released", table_name="legal_holds")
    op.drop_table("legal_holds")
    op.drop_table("signing_keys")
    op.drop_column("organizations", "audit_retention_days")
```

```python
# server/app/compliance/__init__.py
"""Compliance exports: Ed25519 signing, evidence bundles, audit retention and legal holds."""
```

```python
# server/app/compliance/signing.py
"""
Ed25519 keys that sign evidence bundles and deletion receipts.

Production sets AGENTKIT_SIGNING_KEY (base64 32-byte seed) and, optionally,
AGENTKIT_SIGNING_KEY_ID; the private half is never stored. Without it, a key is
generated once and its seed persisted in signing_keys so exports stay verifiable
across restarts. Public keys are never deleted, so rotation doesn't invalidate
old bundles.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
from datetime import datetime
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SigningKey

logger = logging.getLogger("agentkit.cloud.compliance")

ALG = "Ed25519"
KEYS_PATH = "/.well-known/agentkit-signing-keys"
_ENV_KEY = "AGENTKIT_SIGNING_KEY"
_ENV_KID = "AGENTKIT_SIGNING_KEY_ID"


def kid_for(public_key: bytes) -> str:
    return "ak-" + hashlib.sha256(public_key).hexdigest()[:12]


def canonical_bytes(fields: dict[str, Any]) -> bytes:
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str).encode()


def load_env_key() -> tuple[str, Ed25519PrivateKey] | None:
    seed_b64 = os.environ.get(_ENV_KEY)
    if not seed_b64:
        return None
    try:
        private = Ed25519PrivateKey.from_private_bytes(base64.b64decode(seed_b64, validate=True))
    except (ValueError, binascii.Error) as exc:
        raise RuntimeError(f"{_ENV_KEY} must be a base64-encoded 32-byte Ed25519 seed") from exc
    kid = os.environ.get(_ENV_KID) or kid_for(private.public_key().public_bytes_raw())
    return kid, private


async def active_key(db: AsyncSession) -> tuple[str, Ed25519PrivateKey]:
    """The key to sign with now, registering or rotating it as needed."""
    now = datetime.utcnow()
    env = load_env_key()
    if env is not None:
        kid, private = env
        row = await db.get(SigningKey, kid)
        if row is None:
            await _retire_active(db, now)
            db.add(SigningKey(
                kid=kid,
                public_key=_b64(private.public_key().public_bytes_raw()),
                private_key=None,
                source="env",
                active=True,
                created_at=now,
            ))
        elif not row.active:
            await _retire_active(db, now)
            row.active = True
            row.retired_at = None
        return kid, private

    row = (
        await db.execute(
            select(SigningKey).where(SigningKey.active == True, SigningKey.source == "generated")  # noqa: E712
        )
    ).scalars().first()
    if row is not None and row.private_key:
        return row.kid, Ed25519PrivateKey.from_private_bytes(base64.b64decode(row.private_key))

    await _retire_active(db, now)
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    kid = kid_for(public)
    db.add(SigningKey(
        kid=kid,
        public_key=_b64(public),
        private_key=_b64(private.private_bytes_raw()),
        source="generated",
        active=True,
        created_at=now,
    ))
    logger.warning(
        "Signing with a generated key stored in the database (kid %s). "
        "Set %s in production so the private key never touches the database.",
        kid, _ENV_KEY,
    )
    return kid, private


async def sign(db: AsyncSession, data: bytes) -> tuple[str, str]:
    kid, private = await active_key(db)
    return kid, _b64(private.sign(data))


async def public_keys(db: AsyncSession) -> list[dict[str, Any]]:
    rows = (await db.execute(select(SigningKey).order_by(SigningKey.created_at))).scalars().all()
    return [
        {
            "kid": r.kid,
            "alg": ALG,
            "public_key": r.public_key,
            "created_at": r.created_at.isoformat(),
            "retired_at": r.retired_at.isoformat() if r.retired_at else None,
            "active": r.active,
        }
        for r in rows
    ]


async def _retire_active(db: AsyncSession, now: datetime) -> None:
    await db.execute(
        update(SigningKey).where(SigningKey.active == True).values(active=False, retired_at=now)  # noqa: E712
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()
```

```python
# server/app/routers/compliance.py
"""Compliance exports: signing keys, evidence bundles, retention, legal holds, deletion receipts."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.compliance import signing
from app.database import get_db

wellknown_router = APIRouter(tags=["compliance"])
router = APIRouter(prefix="/v1/compliance", tags=["compliance"])


@wellknown_router.get(signing.KEYS_PATH)
async def signing_keys(db: AsyncSession = Depends(get_db)) -> dict[str, list[dict[str, object]]]:
    """Public keys for verifying evidence bundles and deletion receipts. No authentication."""
    await signing.active_key(db)  # ensure a key exists before anyone needs to verify
    await db.commit()
    return {"keys": await signing.public_keys(db)}
```

`server/app/main.py`: import `compliance` router module; `app.include_router(compliance.wellknown_router)` and `app.include_router(compliance.router)`.

- [ ] **Step 4: Run tests and migration**

Run: `cd server && pytest tests/test_compliance.py -v && pytest && ruff check app tests && DATABASE_URL=sqlite+aiosqlite:////tmp/c007.db alembic upgrade head`
Expected: PASS; `006 -> 007`.

- [ ] **Step 5: Commit**

```bash
git add server/pyproject.toml server/app server/migrations/versions/007_compliance.py server/tests/test_compliance.py
git commit -m "feat(server): Ed25519 signing keys with rotation and a public keys endpoint"
```

---

### Task 2: Retention, legal holds, purge with signed receipts

**Files:**
- Create: `server/app/compliance/retention.py`
- Modify: `server/app/routers/compliance.py`, `server/app/schemas.py`, `server/app/main.py`, `server/tests/test_compliance.py`

**Interfaces:**
- Consumes: `signing.sign`, `signing.canonical_bytes` (Task 1).
- Produces: `retention.TIER_RETENTION_DAYS`, `retention.MAX_RETENTION_DAYS = 2555`, `retention.effective_retention(org) -> tuple[int, str]`, `retention.RECEIPT_FIELDS`, `retention.receipt_fields(receipt) -> dict`, `async retention.purge_expired(db, now=None, batch_size=500) -> int`; routes `GET/PUT /v1/compliance/retention`, `GET/POST /v1/compliance/holds`, `POST /v1/compliance/holds/{id}/release`, `GET /v1/compliance/deletions`.

- [ ] **Step 1: Write the failing tests**

```python
# append to server/tests/test_compliance.py
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select

from app.compliance.retention import effective_retention, purge_expired, receipt_fields
from app.models import AuditEvent, AuditRun, DeletionReceipt, LegalHold, Organization


def audit_run(org_id: str, project: str = "prod", completed_days_ago: float | None = 10, created_days_ago: float = 10,
              now: datetime | None = None, events: int = 2) -> tuple[AuditRun, list[AuditEvent]]:
    from app.audit_chain import GENESIS_ROOT, append_event

    now = now or datetime.utcnow()
    run = AuditRun(org_id=org_id, project=project, agent_name="support", run_id=str(uuid.uuid4()),
                   final_root_hash=GENESIS_ROOT, event_count=0, integrity="verified",
                   started_at=now - timedelta(days=created_days_ago),
                   completed_at=now - timedelta(days=completed_days_ago) if completed_days_ago is not None else None,
                   created_at=now - timedelta(days=created_days_ago))
    chain = [append_event(run, event_id=str(uuid.uuid4()), event_type=f"e{i}", actor="a", payload={"i": i},
                          timestamp=now - timedelta(days=created_days_ago, seconds=-i)) for i in range(events)]
    return run, chain


async def add_runs(db, *pairs):
    for run, events in pairs:
        db.add(run)
        db.add_all(events)
    await db.commit()


def test_effective_retention_by_tier():
    assert effective_retention(Organization(name="f", tier="free")) == (7, "tier")
    assert effective_retention(Organization(name="p", tier="pro", audit_retention_days=3000)) == (90, "tier")
    assert effective_retention(Organization(name="e", tier="enterprise")) == (365, "tier")
    assert effective_retention(Organization(name="e", tier="enterprise", audit_retention_days=2555)) == (2555, "override")


async def test_purge_deletes_expired_unheld_runs_with_verifiable_receipts(db, org_and_key):
    org, _ = org_and_key  # free tier: 7 days
    expired = audit_run(org.id, completed_days_ago=10)
    fresh = audit_run(org.id, completed_days_ago=2, created_days_ago=2)
    abandoned = audit_run(org.id, completed_days_ago=None, created_days_ago=30)
    held_project = audit_run(org.id, project="claims", completed_days_ago=10)
    held_run = audit_run(org.id, completed_days_ago=10)
    await add_runs(db, expired, fresh, abandoned, held_project, held_run)
    db.add_all([
        LegalHold(org_id=org.id, project="claims", reason="case #4471"),
        LegalHold(org_id=org.id, run_id=held_run[0].run_id, reason="incident review"),
        LegalHold(org_id=org.id, project="prod", reason="released hold", released_at=datetime.utcnow()),
    ])
    await db.commit()

    purged = await purge_expired(db)

    remaining = set((await db.execute(select(AuditRun.run_id).where(AuditRun.org_id == org.id))).scalars().all())
    assert remaining == {fresh[0].run_id, held_project[0].run_id, held_run[0].run_id}
    assert purged >= 2
    leftover_events = (await db.execute(select(AuditEvent).where(
        AuditEvent.run_id.in_([expired[0].run_id, abandoned[0].run_id])))).scalars().all()
    assert leftover_events == []

    receipts = {r.run_id: r for r in (await db.execute(
        select(DeletionReceipt).where(DeletionReceipt.org_id == org.id))).scalars().all()}
    assert set(receipts) == {expired[0].run_id, abandoned[0].run_id}
    receipt = receipts[expired[0].run_id]
    assert (receipt.final_root_hash, receipt.event_count, receipt.reason) == (expired[0].final_root_hash, 2, "retention")
    key = (await db.execute(select(SigningKey).where(SigningKey.kid == receipt.kid))).scalar_one()
    verify_sig(key.public_key, receipt.signature, signing.canonical_bytes(receipt_fields(receipt)))


async def test_enterprise_override_extends_retention(db, org_and_key):
    org, _ = org_and_key
    org.tier = "enterprise"
    org.audit_retention_days = 2555
    old = audit_run(org.id, completed_days_ago=400)
    await add_runs(db, old)

    await purge_expired(db)

    assert (await db.execute(select(AuditRun).where(AuditRun.run_id == old[0].run_id))).scalar_one_or_none() is not None


async def test_retention_api(client, db, org_and_key):
    org, _ = org_and_key
    assert (await client.get("/v1/compliance/retention")).json() == {
        "tier": "free", "audit_retention_days": 7, "source": "tier", "configurable": False,
    }
    assert (await client.put("/v1/compliance/retention", json={"audit_retention_days": 30})).status_code == 403

    org.tier = "enterprise"
    await db.commit()
    assert (await client.put("/v1/compliance/retention", json={"audit_retention_days": 0})).status_code == 400
    assert (await client.put("/v1/compliance/retention", json={"audit_retention_days": 2556})).status_code == 400
    ok = await client.put("/v1/compliance/retention", json={"audit_retention_days": 2555})
    assert ok.json() == {"tier": "enterprise", "audit_retention_days": 2555, "source": "override", "configurable": True}
    reset = await client.put("/v1/compliance/retention", json={"audit_retention_days": None})
    assert reset.json()["audit_retention_days"] == 365


async def test_holds_api(client, db, org_and_key):
    org, _ = org_and_key
    run, events = audit_run(org.id)
    await add_runs(db, (run, events))

    assert (await client.post("/v1/compliance/holds", json={"reason": "no scope"})).status_code == 400
    assert (await client.post("/v1/compliance/holds", json={"project": "a", "run_id": run.run_id, "reason": "both"})).status_code == 400
    assert (await client.post("/v1/compliance/holds", json={"run_id": str(uuid.uuid4()), "reason": "unknown"})).status_code == 404

    project_hold = (await client.post("/v1/compliance/holds", json={"project": "claims", "reason": "case #4471"})).json()
    run_hold = (await client.post("/v1/compliance/holds", json={"run_id": run.run_id, "reason": "incident"})).json()
    assert project_hold["released_at"] is None

    released = (await client.post(f"/v1/compliance/holds/{run_hold['id']}/release")).json()
    assert released["released_at"] is not None
    holds = (await client.get("/v1/compliance/holds")).json()["holds"]
    assert {h["id"] for h in holds} == {project_hold["id"], run_hold["id"]}
    assert (await client.post(f"/v1/compliance/holds/{uuid.uuid4()}/release")).status_code == 404


async def test_deletions_api(client, db, org_and_key):
    org, _ = org_and_key
    await add_runs(db, audit_run(org.id, completed_days_ago=20))
    await purge_expired(db)

    deletions = (await client.get("/v1/compliance/deletions")).json()["deletions"]

    assert len(deletions) == 1
    assert {"run_id", "final_root_hash", "event_count", "deleted_at", "kid", "signature"} <= set(deletions[0])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && pytest tests/test_compliance.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.compliance.retention'`

- [ ] **Step 3: Implement**

```python
# server/app/compliance/retention.py
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
```

`server/app/schemas.py` — append:

```python
# ---------------------------------------------------------------------------
# Compliance
# ---------------------------------------------------------------------------


class RetentionOut(BaseModel):
    tier: str
    audit_retention_days: int
    source: str
    configurable: bool


class UpdateRetentionRequest(BaseModel):
    audit_retention_days: int | None


class CreateHoldRequest(BaseModel):
    project: str | None = None
    run_id: str | None = None
    reason: str


class HoldOut(BaseModel):
    id: str
    project: str | None
    run_id: str | None
    reason: str
    created_at: datetime
    released_at: datetime | None

    model_config = {"from_attributes": True}


class HoldList(BaseModel):
    holds: list[HoldOut]


class DeletionReceiptOut(BaseModel):
    id: str
    run_id: str
    org_id: str
    project: str
    agent_name: str
    final_root_hash: str
    event_count: int
    chain_origin: str
    started_at: datetime | None
    completed_at: datetime | None
    deleted_at: datetime
    reason: str
    kid: str
    signature: str

    model_config = {"from_attributes": True}


class DeletionList(BaseModel):
    deletions: list[DeletionReceiptOut]
```

`server/app/routers/compliance.py` — add imports and routes:

```python
from datetime import datetime

from fastapi import HTTPException, Query, status
from sqlalchemy import select

from app.auth import get_current_org
from app.compliance.retention import MAX_RETENTION_DAYS, effective_retention
from app.models import AuditRun, DeletionReceipt, LegalHold, Organization
from app.schemas import (
    CreateHoldRequest, DeletionList, DeletionReceiptOut, HoldList, HoldOut, RetentionOut, UpdateRetentionRequest,
)


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
```

`server/app/main.py` worker loop, after `evaluate_all_budgets(db)`:

```python
                        from app.compliance.retention import purge_expired
                        await purge_expired(db)
```

- [ ] **Step 4: Run tests**

Run: `cd server && pytest tests/test_compliance.py -v && pytest && ruff check app tests`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/app server/tests/test_compliance.py
git commit -m "feat(server): audit retention, legal holds, and purge with signed deletion receipts"
```

---

### Task 3: Evidence bundles

**Files:**
- Create: `server/app/compliance/bundle.py`
- Modify: `server/app/routers/compliance.py`, `server/tests/test_compliance.py`

**Interfaces:**
- Consumes: Tasks 1–2; `audit_chain.verify_chain`.
- Produces: `bundle.FORMAT`, `bundle.MAX_RUNS`, `bundle.BundleTooLarge`, `async bundle.build_bundle(org, db, from_, to, project=None, agent_name=None) -> bytes`; route `GET /v1/compliance/export`.

- [ ] **Step 1: Write the failing tests**

```python
# append to server/tests/test_compliance.py
import hashlib
import io
import json
import zipfile

from app.compliance import bundle as bundle_module


def open_bundle(content: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def jsonl(data: bytes) -> list[dict]:
    return [json.loads(line) for line in data.decode().splitlines() if line]


async def test_export_bundle_is_signed_and_self_consistent(client, db, org_and_key):
    org, _ = org_and_key
    now = datetime.utcnow()
    in_range = audit_run(org.id, completed_days_ago=1, created_days_ago=1, now=now, events=3)
    other_project = audit_run(org.id, project="staging", completed_days_ago=1, created_days_ago=1, now=now)
    too_old = audit_run(org.id, completed_days_ago=40, created_days_ago=40, now=now)
    await add_runs(db, in_range, other_project, too_old)
    db.add(LegalHold(org_id=org.id, project="claims", reason="case #4471"))
    await db.commit()

    params = {"from": (now - timedelta(days=2)).isoformat(), "to": now.isoformat()}
    resp = await client.get("/v1/compliance/export", params=params)

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert "attachment" in resp.headers["content-disposition"]
    files = open_bundle(resp.content)
    assert set(files) == {"manifest.json", "manifest.sig", "runs.jsonl", "events.jsonl", "verification.json", "deletions.jsonl"}

    manifest = json.loads(files["manifest.json"])
    sig = json.loads(files["manifest.sig"])
    keys = {k["kid"]: k for k in (await client.get("/.well-known/agentkit-signing-keys")).json()["keys"]}
    verify_sig(keys[sig["kid"]]["public_key"], sig["signature"], files["manifest.json"])
    assert (manifest["format"], sig["alg"], manifest["signing"]["kid"]) == ("agentkit-evidence-bundle/1", "Ed25519", sig["kid"])
    for name, digest in manifest["files"].items():
        assert hashlib.sha256(files[name]).hexdigest() == digest

    runs = jsonl(files["runs.jsonl"])
    assert {r["run_id"] for r in runs} == {in_range[0].run_id, other_project[0].run_id}
    assert manifest["counts"] == {"runs": 2, "events": 5, "deletions": 0}
    assert manifest["retention"] == {"audit_retention_days": 7, "source": "tier"}
    assert [h["project"] for h in manifest["legal_holds"]] == ["claims"]
    events = [e for e in jsonl(files["events.jsonl"]) if e["run_id"] == in_range[0].run_id]
    assert [e["seq"] for e in events] == [0, 1, 2]
    verification = json.loads(files["verification.json"])
    assert (verification["verified"], verification["failed"]) == (2, 0)

    scoped = open_bundle((await client.get("/v1/compliance/export", params={**params, "project": "prod"})).content)
    assert [r["run_id"] for r in jsonl(scoped["runs.jsonl"])] == [in_range[0].run_id]


async def test_export_reports_a_tampered_stored_chain(client, db, org_and_key):
    org, _ = org_and_key
    now = datetime.utcnow()
    run, events = audit_run(org.id, completed_days_ago=1, created_days_ago=1, now=now)
    events[1].payload_hash = "f" * 64
    await add_runs(db, (run, events))

    resp = await client.get("/v1/compliance/export", params={"from": (now - timedelta(days=2)).isoformat(), "to": now.isoformat()})

    verification = json.loads(open_bundle(resp.content)["verification.json"])
    assert verification["failed"] == 1
    assert verification["runs"][0] == {"run_id": run.run_id, "verified": False, "broken_seq": 1}


async def test_export_includes_deletion_receipts_in_range(client, db, org_and_key):
    org, _ = org_and_key
    await add_runs(db, audit_run(org.id, completed_days_ago=20))
    await purge_expired(db)
    now = datetime.utcnow()

    resp = await client.get("/v1/compliance/export", params={"from": (now - timedelta(hours=1)).isoformat(),
                                                             "to": (now + timedelta(hours=1)).isoformat()})

    files = open_bundle(resp.content)
    (receipt,) = jsonl(files["deletions.jsonl"])
    assert json.loads(files["manifest.json"])["counts"]["deletions"] == 1
    assert {"kid", "signature", "final_root_hash"} <= set(receipt)


async def test_export_rejects_bad_ranges_and_oversized_scopes(client, db, org_and_key, monkeypatch):
    org, _ = org_and_key
    now = datetime.utcnow()
    await add_runs(db, audit_run(org.id, completed_days_ago=1, created_days_ago=1, now=now))
    params = {"from": (now - timedelta(days=2)).isoformat(), "to": now.isoformat()}

    same = now.isoformat()
    assert (await client.get("/v1/compliance/export", params={"from": same, "to": same})).status_code == 400

    monkeypatch.setattr(bundle_module, "MAX_RUNS", 0)
    resp = await client.get("/v1/compliance/export", params=params)
    assert resp.status_code == 400
    assert "at most 0" in resp.json()["detail"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && pytest tests/test_compliance.py -v -k export`
Expected: FAIL — `ImportError: cannot import name 'bundle'` / `404`

- [ ] **Step 3: Implement**

```python
# server/app/compliance/bundle.py
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
```

`server/app/routers/compliance.py` — add (imports consolidated at the top of the module, including `from datetime import timezone`):

```python
from fastapi import Response

from app.compliance.bundle import BundleTooLarge, build_bundle


@router.get("/export")
async def export_bundle(
    from_: datetime = Query(..., alias="from"),
    to: datetime = Query(...),
    project: str | None = Query(None),
    agent_name: str | None = Query(None),
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """A signed evidence bundle (zip) of audit chains started in [from, to)."""
    from_, to = _naive_utc(from_), _naive_utc(to)
    if from_ >= to:
        raise HTTPException(status_code=400, detail="'from' must be earlier than 'to'.")
    try:
        content = await build_bundle(org, db, from_, to, project, agent_name)
    except BundleTooLarge as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()  # persists a newly generated signing key, if one was created
    filename = f"agentkit-evidence-{from_.date().isoformat()}-{to.date().isoformat()}.zip"
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
```

- [ ] **Step 4: Run tests**

Run: `cd server && pytest tests/test_compliance.py -v && pytest && ruff check app tests`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/app server/tests/test_compliance.py
git commit -m "feat(server): signed evidence bundle export"
```

---

### Task 4: SDK verifier and `agent-kit verify`

**Files:**
- Create: `agent_kit/compliance.py`, `agent_kit/cli.py`, `tests/test_compliance.py`
- Modify: `pyproject.toml`, `.github/workflows/ci.yml`

**Interfaces:**
- Produces: `BundleReport`, `load_public_keys(source, http_client=None) -> dict[str, bytes]`, `verify_bundle(path, public_keys) -> BundleReport`, `receipt_bytes(receipt: dict) -> bytes`, `RECEIPT_FIELDS`; `cli.main(argv=None) -> int`; console script `agent-kit`; extra `compliance`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_compliance.py
"""Offline verification of agent-kit evidence bundles."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest

cryptography = pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from agent_kit.audit.chain import AuditChain  # noqa: E402
from agent_kit.cli import main  # noqa: E402
from agent_kit.compliance import load_public_keys, receipt_bytes, verify_bundle  # noqa: E402

KID = "ak-test"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def make_bundle(tmp_path: Path, key: Ed25519PrivateKey, *, tamper: str | None = None) -> Path:
    """Build a bundle exactly as the server does (format agentkit-evidence-bundle/1)."""
    runs, events = [], []
    for n in range(2):
        chain = AuditChain()
        for i in range(3):
            chain.append(f"event_{i}", actor="agent", payload={"n": n, "i": i})
        run_id = f"run-{n}"
        runs.append({"run_id": run_id, "project": "prod", "agent_name": "support", "chain_origin": "client",
                     "started_at": None, "completed_at": None, "event_count": len(chain),
                     "final_root_hash": chain.root_hash(), "integrity": "verified"})
        for seq, e in enumerate(chain.events()):
            events.append({"run_id": run_id, "seq": seq, "event_id": e.event_id, "event_type": e.event_type,
                           "actor": e.actor, "payload_hash": e.payload_hash, "prev_root": e.prev_root,
                           "leaf_hash": e.leaf_hash, "timestamp": e.timestamp.isoformat()})

    receipt = {"run_id": "old-run", "org_id": "org", "project": "prod", "agent_name": "support",
               "final_root_hash": "a" * 64, "event_count": 4, "chain_origin": "client",
               "started_at": "2026-01-01T00:00:00", "completed_at": "2026-01-01T00:01:00",
               "deleted_at": "2026-09-14T00:00:00", "reason": "retention"}
    receipt_sig = key.sign(receipt_bytes(receipt))
    if tamper == "receipt":
        receipt["event_count"] = 5
    deletions = [{**receipt, "kid": KID, "signature": b64(receipt_sig)}]

    if tamper == "event":
        events[1]["payload_hash"] = "0" * 64

    def jsonl(rows: list[dict[str, Any]]) -> bytes:
        return "".join(json.dumps(r) + "\n" for r in rows).encode()

    files = {
        "runs.jsonl": jsonl(runs),
        "events.jsonl": jsonl(events),
        "verification.json": json.dumps({"runs": [], "verified": 2, "failed": 0}).encode(),
        "deletions.jsonl": jsonl(deletions),
    }
    manifest = {"format": "agentkit-evidence-bundle/1", "counts": {"runs": 2, "events": 6, "deletions": 1},
                "files": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()},
                "signing": {"kid": KID, "alg": "Ed25519"}}
    if tamper == "event":  # attacker also fixes the file hash, but can't re-sign
        manifest["files"]["events.jsonl"] = hashlib.sha256(files["events.jsonl"]).hexdigest()
    manifest_bytes = json.dumps(manifest, indent=2).encode()
    signature = key.sign(manifest_bytes)
    if tamper == "manifest":
        manifest_bytes = manifest_bytes.replace(b'"runs": 2', b'"runs": 3')
    if tamper == "hash-only":
        files["runs.jsonl"] += b"\n"

    path = tmp_path / "bundle.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", manifest_bytes)
        zf.writestr("manifest.sig", json.dumps({"kid": KID, "alg": "Ed25519", "signature": b64(signature)}))
        for name, content in files.items():
            if tamper == "missing" and name == "events.jsonl":
                continue
            zf.writestr(name, content)
    return path


@pytest.fixture
def key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def keys_for(key: Ed25519PrivateKey) -> dict[str, bytes]:
    return {KID: key.public_key().public_bytes_raw()}


def test_valid_bundle_verifies(tmp_path, key):
    report = verify_bundle(make_bundle(tmp_path, key), keys_for(key))
    assert report.ok, report.errors
    assert (report.kid, report.signature_valid, report.files_valid) == (KID, True, True)
    assert (report.runs_total, report.runs_verified, report.deletions_total, report.deletions_verified) == (2, 2, 1, 1)


@pytest.mark.parametrize(("tamper", "expect"), [
    ("manifest", "signature"),
    ("event", "chain"),
    ("hash-only", "sha256"),
    ("missing", "missing"),
    ("receipt", "receipt"),
])
def test_tampering_is_detected(tmp_path, key, tamper, expect):
    report = verify_bundle(make_bundle(tmp_path, key, tamper=tamper), keys_for(key))
    assert not report.ok
    assert any(expect in error for error in report.errors), report.errors


def test_wrong_or_unknown_key_fails(tmp_path, key):
    path = make_bundle(tmp_path, key)
    other = Ed25519PrivateKey.generate()
    assert not verify_bundle(path, {KID: other.public_key().public_bytes_raw()}).signature_valid
    unknown = verify_bundle(path, {"ak-other": key.public_key().public_bytes_raw()})
    assert not unknown.signature_valid and any("unknown signing key" in e for e in unknown.errors)


def keys_document(key: Ed25519PrivateKey) -> dict[str, Any]:
    return {"keys": [{"kid": KID, "alg": "Ed25519", "public_key": b64(key.public_key().public_bytes_raw()), "active": True}]}


def test_load_public_keys_from_file_and_url(tmp_path, key):
    path = tmp_path / "keys.json"
    path.write_text(json.dumps(keys_document(key)))
    assert load_public_keys(str(path)) == keys_for(key)

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=keys_document(key)))
    assert load_public_keys("https://cloud.test/.well-known/agentkit-signing-keys",
                            http_client=httpx.Client(transport=transport)) == keys_for(key)


def test_cli_exit_codes(tmp_path, key, capsys):
    keys_path = tmp_path / "keys.json"
    keys_path.write_text(json.dumps(keys_document(key)))
    good = make_bundle(tmp_path, key)

    assert main(["verify", str(good), "--public-key", str(keys_path)]) == 0
    assert "✔" in capsys.readouterr().out

    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    bad = make_bundle(bad_dir, key, tamper="event")
    assert main(["verify", str(bad), "--public-key", str(keys_path)]) == 1
    assert "✘" in capsys.readouterr().out

    assert main(["verify", str(tmp_path / "nope.zip"), "--public-key", str(keys_path)]) == 2
    with pytest.raises(SystemExit) as usage:
        main(["verify", str(good)])
    assert usage.value.code == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_compliance.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent_kit.cli'`

- [ ] **Step 3: Implement**

```python
# agent_kit/compliance.py
"""
Verify agent-kit evidence bundles offline.

    from agent_kit.compliance import load_public_keys, verify_bundle
    keys = load_public_keys("https://ingest.agentkit.io/.well-known/agentkit-signing-keys")
    report = verify_bundle("agentkit-evidence-2026-09-01-2026-10-01.zip", keys)

Checks the manifest signature, every file's SHA-256, every run's hash chain, and every
deletion receipt's signature. Requires ``pip install agent-kit[compliance]``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from agent_kit.audit.chain import _GENESIS_ROOT, _compute_leaf_hash

FORMAT = "agentkit-evidence-bundle/1"
DATA_FILES = ("runs.jsonl", "events.jsonl", "verification.json", "deletions.jsonl")
RECEIPT_FIELDS = (
    "run_id", "org_id", "project", "agent_name", "final_root_hash", "event_count",
    "chain_origin", "started_at", "completed_at", "deleted_at", "reason",
)


@dataclass
class BundleReport:
    ok: bool = False
    kid: str | None = None
    signature_valid: bool = False
    files_valid: bool = False
    runs_total: int = 0
    runs_verified: int = 0
    deletions_total: int = 0
    deletions_verified: int = 0
    errors: list[str] = field(default_factory=list)


def receipt_bytes(receipt: dict[str, Any]) -> bytes:
    """The bytes a deletion receipt's signature covers."""
    fields = {name: receipt.get(name) for name in RECEIPT_FIELDS}
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str).encode()


def load_public_keys(source: str, http_client: httpx.Client | None = None) -> dict[str, bytes]:
    """Load Ed25519 public keys by kid from a keys URL or a JSON file in the same format."""
    if source.startswith(("http://", "https://")):
        client = http_client or httpx.Client(timeout=10.0)
        response = client.get(source)
        response.raise_for_status()
        document = response.json()
    else:
        document = json.loads(Path(source).read_text())
    keys = document.get("keys", []) if isinstance(document, dict) else document
    return {
        str(k["kid"]): base64.b64decode(k["public_key"])
        for k in keys
        if isinstance(k, dict) and k.get("alg", "Ed25519") == "Ed25519"
    }


def verify_bundle(path: str | Path, public_keys: dict[str, bytes]) -> BundleReport:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise ImportError(
            "Verifying evidence bundles requires 'cryptography'. Install it with: pip install agent-kit[compliance]"
        ) from exc

    report = BundleReport()

    def signature_ok(kid: str, signature_b64: str, data: bytes) -> bool:
        public = public_keys.get(kid)
        if public is None:
            return False
        try:
            Ed25519PublicKey.from_public_bytes(public).verify(base64.b64decode(signature_b64), data)
            return True
        except (InvalidSignature, ValueError):
            return False

    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        if "manifest.json" not in names or "manifest.sig" not in names:
            report.errors.append("missing manifest.json or manifest.sig")
            return report
        manifest_bytes = zf.read("manifest.json")
        contents = {name: zf.read(name) for name in DATA_FILES if name in names}
        sig = json.loads(zf.read("manifest.sig"))

    report.kid = str(sig.get("kid"))
    if report.kid not in public_keys:
        report.errors.append(f"unknown signing key {report.kid!r}")
    report.signature_valid = signature_ok(report.kid, str(sig.get("signature", "")), manifest_bytes)
    if not report.signature_valid and report.kid in public_keys:
        report.errors.append("manifest signature is invalid")

    manifest = json.loads(manifest_bytes)
    if manifest.get("format") != FORMAT:
        report.errors.append(f"unsupported bundle format {manifest.get('format')!r}")

    report.files_valid = True
    for name, digest in manifest.get("files", {}).items():
        if name not in contents:
            report.files_valid = False
            report.errors.append(f"{name} is missing")
        elif hashlib.sha256(contents[name]).hexdigest() != digest:
            report.files_valid = False
            report.errors.append(f"{name} sha256 does not match the manifest")

    runs = _jsonl(contents.get("runs.jsonl", b""))
    events_by_run: dict[str, list[dict[str, Any]]] = {}
    for event in _jsonl(contents.get("events.jsonl", b"")):
        events_by_run.setdefault(str(event["run_id"]), []).append(event)
    report.runs_total = len(runs)
    for run in runs:
        problem = _chain_problem(run, sorted(events_by_run.get(str(run["run_id"]), []), key=lambda e: e["seq"]))
        if problem is None:
            report.runs_verified += 1
        else:
            report.errors.append(f"run {run['run_id']}: chain {problem}")

    receipts = _jsonl(contents.get("deletions.jsonl", b""))
    report.deletions_total = len(receipts)
    for receipt in receipts:
        if signature_ok(str(receipt.get("kid")), str(receipt.get("signature", "")), receipt_bytes(receipt)):
            report.deletions_verified += 1
        else:
            report.errors.append(f"deletion receipt for run {receipt.get('run_id')} has an invalid signature")

    report.ok = (
        report.signature_valid
        and report.files_valid
        and report.runs_verified == report.runs_total
        and report.deletions_verified == report.deletions_total
        and not report.errors
    )
    return report


def _chain_problem(run: dict[str, Any], events: list[dict[str, Any]]) -> str | None:
    root = _GENESIS_ROOT
    for event in events:
        if event["prev_root"] != root:
            return f"broken at seq {event['seq']}: prev_root mismatch"
        expected = _compute_leaf_hash(
            root, event["event_type"], event["payload_hash"], datetime.fromisoformat(event["timestamp"])
        )
        if expected != event["leaf_hash"]:
            return f"broken at seq {event['seq']}: leaf hash mismatch"
        root = event["leaf_hash"]
    if len(events) != run["event_count"]:
        return f"has {len(events)} events, run records {run['event_count']}"
    if root != run["final_root_hash"]:
        return "final root does not match the run"
    return None


def _jsonl(data: bytes) -> list[dict[str, Any]]:
    return [json.loads(line) for line in data.decode().splitlines() if line.strip()]
```

```python
# agent_kit/cli.py
"""agent-kit command-line tools."""

from __future__ import annotations

import argparse
import sys
import zipfile

import httpx


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-kit")
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify", help="Verify a signed agent-kit evidence bundle offline")
    verify.add_argument("bundle", help="Path to the evidence bundle zip")
    source = verify.add_mutually_exclusive_group(required=True)
    source.add_argument("--keys-url", help="agent-kit signing keys URL (…/.well-known/agentkit-signing-keys)")
    source.add_argument("--public-key", help="JSON file of signing keys in the keys-URL format")
    args = parser.parse_args(argv)

    try:
        from agent_kit.compliance import load_public_keys, verify_bundle

        keys = load_public_keys(args.keys_url or args.public_key)
        report = verify_bundle(args.bundle, keys)
    except ImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, httpx.HTTPError, zipfile.BadZipFile) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    def line(ok: bool, text: str) -> None:
        print(f"{'✔' if ok else '✘'} {text}")

    line(report.signature_valid, f"manifest signature (kid {report.kid})")
    line(report.files_valid, "file hashes match the manifest")
    line(report.runs_verified == report.runs_total, f"audit chains: {report.runs_verified}/{report.runs_total} verified")
    line(report.deletions_verified == report.deletions_total,
         f"deletion receipts: {report.deletions_verified}/{report.deletions_total} verified")
    for error in report.errors:
        print(f"  - {error}")
    print("VERIFIED" if report.ok else "FAILED")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
```

`pyproject.toml`:

```toml
compliance       = ["cryptography>=41"]
all              = ["agent-kit[openai,ollama,otel,claude-agent-sdk,openai-agents,compliance]"]

[project.scripts]
agent-kit = "agent_kit.cli:main"
```

CI SDK install: `pip install -e ".[dev,openai,otel,claude-agent-sdk,openai-agents,compliance]"`.

- [ ] **Step 4: Run tests** (with and without `cryptography`)

Run: `pytest tests/test_compliance.py -v && pytest && ruff check agent_kit tests && mypy agent_kit`
Expected: PASS / clean (module skipped without `cryptography`)

- [ ] **Step 5: Commit**

```bash
git add agent_kit/compliance.py agent_kit/cli.py tests/test_compliance.py pyproject.toml .github/workflows/ci.yml
git commit -m "feat: agent-kit verify — offline evidence bundle verification"
```

---

### Task 5: Docs and end-to-end check

**Files:**
- Modify: `docs/api-reference.md`, `docs/cloud-quickstart.md`, `docs/self-hosting.md`, `README.md`, `CHANGELOG.md`, `specs/06-harness-roadmap.md`, `specs/10-compliance-exports.md`, `PROJECT_INDEX.md`

- [ ] **Step 1: Docs**
  - `docs/api-reference.md`: `## Compliance` — keys endpoint, export (params, bundle layout table, 400s), retention GET/PUT (tier table), holds, deletions (receipt fields and canonical-bytes definition).
  - `docs/cloud-quickstart.md`: `## Evidence for auditors` — export with curl, verify with `pip install agent-kit[compliance]` + `agent-kit verify bundle.zip --keys-url …`, retention and holds in two short examples, wording "supports record-keeping obligations (EU AI Act Art. 12, SOC 2); not a certification".
  - `docs/self-hosting.md`: env table rows `AGENTKIT_SIGNING_KEY` (base64 32-byte Ed25519 seed; generate with `python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"`), `AGENTKIT_SIGNING_KEY_ID`; note retention purge runs with the alert worker.
  - `README.md`: "Built in" row `Evidence bundles | Signed, offline-verifiable exports of audit chains with retention, legal holds, and deletion receipts`.
  - `CHANGELOG.md` `[Unreleased]` → `### Added`: compliance exports entry (bundles, verify CLI + extra, retention/holds/receipts, migration 007, server `cryptography` dependency).
  - Spec 10 status `implemented`; roadmap `3.3` ticked; `PROJECT_INDEX.md` entries (compliance package, router, migration 007, CLI, tests, spec 10).

- [ ] **Step 2: End-to-end**

Fresh migrated SQLite database, seeded key, `AGENTKIT_SIGNING_KEY` set, server running. Report two SDK agent runs through `CloudReporter`. `curl` the export for today → `agent-kit verify bundle.zip --keys-url http://127.0.0.1:<port>/.well-known/agentkit-signing-keys` exits 0. Flip one character of a `leaf_hash` in `events.jsonl` and re-zip → exits 1 naming the chain. Backdate one run's `created_at`/`completed_at` by 30 days and put a hold on the other run's project with both backdated; call `purge_expired` against the database → the unheld run is gone, the held one remains; export a bundle covering today → verify passes with `deletion receipts: 1/1`.

- [ ] **Step 3: Commit**

```bash
git add docs README.md CHANGELOG.md specs PROJECT_INDEX.md
git commit -m "docs: compliance exports — evidence bundles, verify CLI, retention"
```
