"""Server-built audit chains and chain_origin exposure."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from app.audit_chain import GENESIS_ROOT, append_event, payload_hash, verify_chain
from app.models import AuditRun


def test_payload_hash_matches_sdk_serialisation():
    import hashlib
    import json

    payload = {"b": 2, "a": {"z": 1, "y": [1, 2]}}
    expected = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    assert payload_hash(payload) == expected


def test_append_event_builds_a_verifiable_chain():
    run = AuditRun(
        org_id="org", project="p", agent_name="a", run_id=str(uuid.uuid4()),
        final_root_hash=GENESIS_ROOT, event_count=0, chain_origin="ingest",
    )
    t0 = datetime(2026, 9, 13, 12, 0, 0, 123456)
    events = [
        append_event(run, event_id=str(uuid.uuid4()), event_type=kind, actor="x",
                     payload={"i": i}, timestamp=t0 + timedelta(seconds=i))
        for i, kind in enumerate(["agent_start", "llm_complete", "tool_call", "agent_complete"])
    ]

    assert [e.seq for e in events] == [0, 1, 2, 3]
    assert events[0].prev_root == GENESIS_ROOT
    assert run.final_root_hash == events[-1].leaf_hash
    assert run.event_count == 4
    assert verify_chain(events) == (True, None, None, None)


async def test_audit_runs_expose_chain_origin(client, db, org_and_key):
    org, _ = org_and_key
    db.add(AuditRun(org_id=org.id, project="p", agent_name="sdk-agent", run_id=str(uuid.uuid4()),
                    final_root_hash=GENESIS_ROOT, event_count=0))
    await db.commit()

    resp = await client.get("/v1/audit/runs")

    assert resp.status_code == 200
    assert resp.json()["runs"][0]["chain_origin"] == "client"
