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
