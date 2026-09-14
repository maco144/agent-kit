"""Compliance exports: signing keys, retention, holds, purge receipts, evidence bundles."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import uuid
import zipfile
from datetime import datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.compliance import bundle as bundle_module
from app.compliance import signing
from app.compliance.retention import effective_retention, purge_expired, receipt_fields
from app.models import AuditEvent, AuditRun, DeletionReceipt, LegalHold, Organization, SigningKey


def verify_sig(public_key_b64: str, signature_b64: str, data: bytes) -> None:
    Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64)).verify(base64.b64decode(signature_b64), data)


async def keys_by_kid(db) -> dict[str, SigningKey]:
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
