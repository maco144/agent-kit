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
