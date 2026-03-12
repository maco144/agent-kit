"""Integration tests for the ingest API and audit trail endpoints."""

from __future__ import annotations

import gzip
import json
import uuid
from datetime import datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.main import app
from app.models import AuditEvent, AuditRun, CloudEventLog, Organization


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_run_start_event(run_id: str, agent_name: str = "test-agent") -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": "run_start",
        "run_id": run_id,
        "agent_name": agent_name,
        "project": "test",
        "occurred_at": datetime.utcnow().isoformat(),
        "payload": {"model": "claude-sonnet-4-6", "prompt_hash": "abc123"},
    }


def make_audit_flush_event(run_id: str, chain_events: list[dict]) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": "audit_flush",
        "run_id": run_id,
        "agent_name": "test-agent",
        "project": "test",
        "occurred_at": datetime.utcnow().isoformat(),
        "payload": {
            "final_root_hash": chain_events[-1]["leaf_hash"] if chain_events else "0" * 64,
            "event_count": len(chain_events),
            "events": chain_events,
        },
    }


def build_audit_chain(n: int = 3) -> list[dict]:
    """Build a valid Merkle chain of n events."""
    import hashlib as _hl
    events = []
    root = "0" * 64

    for i in range(n):
        event_type = "agent_start" if i == 0 else "tool_call" if i < n - 1 else "agent_complete"
        actor = "test-agent"
        payload_hash = _hl.sha256(f"payload_{i}".encode()).hexdigest()
        ts = datetime.utcnow().replace(microsecond=i * 1000)
        raw = root + event_type + payload_hash + ts.isoformat()
        leaf_hash = _hl.sha256(raw.encode()).hexdigest()

        events.append({
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "actor": actor,
            "payload_hash": payload_hash,
            "prev_root": root,
            "leaf_hash": leaf_hash,
            "timestamp": ts.isoformat(),
        })
        root = leaf_hash

    return events


def ndjson_body(events: list[dict]) -> bytes:
    ndjson = "\n".join(json.dumps(e) for e in events).encode()
    return gzip.compress(ndjson)


# ---------------------------------------------------------------------------
# Ingest tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_ingest_requires_auth():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as anon:
        resp = await anon.post(
            "/v1/events",
            content=ndjson_body([make_run_start_event("r1")]),
            headers={"Content-Encoding": "gzip", "Content-Type": "application/x-ndjson"},
        )
    assert resp.status_code == 401  # HTTPBearer raises 401 when no credentials


@pytest.mark.asyncio
async def test_ingest_run_start_stored(client, db, org_and_key):
    org, _ = org_and_key
    run_id = str(uuid.uuid4())
    events = [make_run_start_event(run_id)]

    resp = await client.post(
        "/v1/events",
        content=ndjson_body(events),
        headers={"Content-Encoding": "gzip", "Content-Type": "application/x-ndjson"},
    )
    assert resp.status_code == 202
    assert resp.json()["accepted"] == 1

    result = await db.execute(
        select(CloudEventLog).where(CloudEventLog.run_id == run_id)
    )
    row = result.scalar_one_or_none()
    assert row is not None
    assert row.event_type == "run_start"
    assert row.org_id == org.id


@pytest.mark.asyncio
async def test_ingest_audit_flush_stores_run_and_events(client, db, org_and_key):
    org, _ = org_and_key
    run_id = str(uuid.uuid4())
    chain = build_audit_chain(4)
    events = [
        make_run_start_event(run_id),
        make_audit_flush_event(run_id, chain),
    ]

    resp = await client.post(
        "/v1/events",
        content=ndjson_body(events),
        headers={"Content-Encoding": "gzip", "Content-Type": "application/x-ndjson"},
    )
    assert resp.status_code == 202

    # AuditRun should be created
    run_result = await db.execute(
        select(AuditRun).where(AuditRun.run_id == run_id)
    )
    run = run_result.scalar_one_or_none()
    assert run is not None
    assert run.event_count == 4
    assert run.org_id == org.id

    # AuditEvents should be stored
    events_result = await db.execute(
        select(AuditEvent).where(AuditEvent.run_id == run_id)
    )
    stored_events = list(events_result.scalars().all())
    assert len(stored_events) == 4


@pytest.mark.asyncio
async def test_ingest_idempotent(client, db):
    """Sending the same batch twice should not create duplicate records."""
    run_id = str(uuid.uuid4())
    chain = build_audit_chain(2)
    batch = ndjson_body([make_audit_flush_event(run_id, chain)])
    headers = {"Content-Encoding": "gzip", "Content-Type": "application/x-ndjson"}

    await client.post("/v1/events", content=batch, headers=headers)
    await client.post("/v1/events", content=batch, headers=headers)

    events_result = await db.execute(
        select(AuditEvent).where(AuditEvent.run_id == run_id)
    )
    stored = list(events_result.scalars().all())
    assert len(stored) == 2  # not 4


@pytest.mark.asyncio
async def test_ingest_rejects_invalid_json(client):
    resp = await client.post(
        "/v1/events",
        content=gzip.compress(b"not json at all\n"),
        headers={"Content-Encoding": "gzip", "Content-Type": "application/x-ndjson"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Audit trail API tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_runs_empty(client):
    resp = await client.get("/v1/audit/runs")
    assert resp.status_code == 200
    data = resp.json()
    assert "runs" in data
    assert isinstance(data["runs"], list)


@pytest.mark.asyncio
async def test_get_run_detail(client, db, org_and_key):
    org, _ = org_and_key
    run_id = str(uuid.uuid4())
    chain = build_audit_chain(3)

    await client.post(
        "/v1/events",
        content=ndjson_body([make_run_start_event(run_id), make_audit_flush_event(run_id, chain)]),
        headers={"Content-Encoding": "gzip", "Content-Type": "application/x-ndjson"},
    )

    resp = await client.get(f"/v1/audit/runs/{run_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["run_id"] == run_id
    assert len(data["events"]) == 3


@pytest.mark.asyncio
async def test_get_run_not_found(client):
    resp = await client.get(f"/v1/audit/runs/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_verify_run_valid_chain(client):
    run_id = str(uuid.uuid4())
    chain = build_audit_chain(5)
    await client.post(
        "/v1/events",
        content=ndjson_body([make_audit_flush_event(run_id, chain)]),
        headers={"Content-Encoding": "gzip", "Content-Type": "application/x-ndjson"},
    )

    resp = await client.get(f"/v1/audit/runs/{run_id}/verify")
    assert resp.status_code == 200
    data = resp.json()
    assert data["verified"] is True
    assert data["event_count"] == 5


@pytest.mark.asyncio
async def test_verify_run_tampered_chain(client, db):
    """A chain with a tampered hash should fail verification."""
    run_id = str(uuid.uuid4())
    chain = build_audit_chain(3)
    # Tamper: replace leaf_hash of event 1 with garbage
    chain[1]["leaf_hash"] = "deadbeef" * 8

    await client.post(
        "/v1/events",
        content=ndjson_body([make_audit_flush_event(run_id, chain)]),
        headers={"Content-Encoding": "gzip", "Content-Type": "application/x-ndjson"},
    )

    resp = await client.get(f"/v1/audit/runs/{run_id}/verify")
    assert resp.status_code == 200
    data = resp.json()
    assert data["verified"] is False
    assert "broken_at_seq" in data


@pytest.mark.asyncio
async def test_export_jsonl(client):
    run_id = str(uuid.uuid4())
    chain = build_audit_chain(3)
    await client.post(
        "/v1/events",
        content=ndjson_body([make_audit_flush_event(run_id, chain)]),
        headers={"Content-Encoding": "gzip", "Content-Type": "application/x-ndjson"},
    )

    resp = await client.get(f"/v1/audit/runs/{run_id}/export?format=jsonl")
    assert resp.status_code == 200
    assert "application/x-ndjson" in resp.headers["content-type"]
    lines = [l for l in resp.text.splitlines() if l]
    assert len(lines) == 3
    obj = json.loads(lines[0])
    assert "leaf_hash" in obj
    assert "prev_root" in obj


@pytest.mark.asyncio
async def test_export_csv(client):
    run_id = str(uuid.uuid4())
    chain = build_audit_chain(2)
    await client.post(
        "/v1/events",
        content=ndjson_body([make_audit_flush_event(run_id, chain)]),
        headers={"Content-Encoding": "gzip", "Content-Type": "application/x-ndjson"},
    )

    resp = await client.get(f"/v1/audit/runs/{run_id}/export?format=csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    lines = resp.text.splitlines()
    assert lines[0].startswith("seq,event_id")
    assert len(lines) == 3  # header + 2 events


@pytest.mark.asyncio
async def test_list_events_filter_by_event_type(client):
    run_id = str(uuid.uuid4())
    chain = build_audit_chain(3)
    await client.post(
        "/v1/events",
        content=ndjson_body([make_audit_flush_event(run_id, chain)]),
        headers={"Content-Encoding": "gzip", "Content-Type": "application/x-ndjson"},
    )

    resp = await client.get("/v1/audit/events?event_type=agent_start")
    assert resp.status_code == 200
    data = resp.json()
    assert all(e["event_type"] == "agent_start" for e in data["events"])
