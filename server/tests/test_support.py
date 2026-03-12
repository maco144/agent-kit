"""Integration tests for the support context API."""

from __future__ import annotations

import gzip
import json
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import AgentMetricSnapshot, AlertFiring, AlertRule, AuditRun, CircuitBreakerEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def ndjson_body(events: list[dict]) -> bytes:
    return gzip.compress("\n".join(json.dumps(e) for e in events).encode())


def _h() -> dict:
    return {"Content-Encoding": "gzip", "Content-Type": "application/x-ndjson"}


def _run_start(run_id: str, agent: str = "billing-agent") -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": "run_start",
        "run_id": run_id,
        "agent_name": agent,
        "project": "prod",
        "occurred_at": datetime.utcnow().isoformat(),
        "payload": {"model": "claude-sonnet-4-6", "prompt_hash": "x"},
    }


def _run_complete(run_id: str, agent: str = "billing-agent") -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": "run_complete",
        "run_id": run_id,
        "agent_name": agent,
        "project": "prod",
        "occurred_at": datetime.utcnow().isoformat(),
        "payload": {"total_cost_usd": 0.05, "total_tokens": 300, "total_turns": 2,
                    "audit_root_hash": "0" * 64},
    }


def _turn(run_id: str, agent: str = "billing-agent") -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": "turn_complete",
        "run_id": run_id,
        "agent_name": agent,
        "project": "prod",
        "occurred_at": datetime.utcnow().isoformat(),
        "payload": {"turn_index": 0, "input_tokens": 200, "output_tokens": 100,
                    "cost_usd": 0.05, "duration_ms": 1500, "tool_names": []},
    }


# ---------------------------------------------------------------------------
# SLA endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sla_default_tier_is_free(client, org_and_key):
    resp = await client.get("/v1/support/sla")
    assert resp.status_code == 200
    data = resp.json()
    assert data["tier"] == "free"
    assert data["p1_response_hours"] is None
    assert data["p1_coverage"] == "none"


@pytest.mark.asyncio
async def test_sla_changes_after_tier_upgrade(client):
    await client.patch("/v1/support/tier", json={"tier": "enterprise"})
    resp = await client.get("/v1/support/sla")
    data = resp.json()
    assert data["tier"] == "enterprise"
    assert data["p1_response_hours"] == 1
    assert data["p1_coverage"] == "24/7"
    assert data["p2_response_hours"] == 4
    assert data["max_contacts"] is None  # unlimited

    # Reset to free for other tests
    await client.patch("/v1/support/tier", json={"tier": "free"})


@pytest.mark.asyncio
async def test_pro_sla(client):
    await client.patch("/v1/support/tier", json={"tier": "pro"})
    resp = await client.get("/v1/support/sla")
    data = resp.json()
    assert data["tier"] == "pro"
    assert data["p1_response_hours"] == 4
    assert data["p2_response_hours"] == 8
    assert data["p3_response_hours"] == 24
    assert data["max_contacts"] == 3
    await client.patch("/v1/support/tier", json={"tier": "free"})


# ---------------------------------------------------------------------------
# Tier management
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_tier_with_metadata(client):
    resp = await client.patch(
        "/v1/support/tier",
        json={
            "tier": "enterprise",
            "plan_metadata": {
                "cse_name": "Jane Smith",
                "slack_channel": "#agentkit-support-acme",
                "contract_id": "ENT-0042",
            },
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["tier"] == "enterprise"
    assert data["plan_metadata"]["cse_name"] == "Jane Smith"
    assert data["sla"]["p1_response_hours"] == 1

    await client.patch("/v1/support/tier", json={"tier": "free"})


@pytest.mark.asyncio
async def test_update_tier_invalid(client):
    resp = await client.patch("/v1/support/tier", json={"tier": "diamond"})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Support context — empty org
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_empty_org(client):
    resp = await client.get("/v1/support/context")
    assert resp.status_code == 200
    data = resp.json()
    assert data["metrics"]["total_runs"] == 0
    assert data["metrics"]["active_runs"] == 0
    assert data["circuit_breaker"]["open_agents"] == []
    assert data["alerts"]["firing_count"] == 0
    assert data["audit"]["total_runs"] == 0
    assert data["agents"] == []
    assert "generated_at" in data
    assert data["period_hours"] == 24


# ---------------------------------------------------------------------------
# Support context — with fleet data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_metrics_populated(client, db):
    agent = f"ctx-agent-{uuid.uuid4().hex[:6]}"
    run_id = str(uuid.uuid4())
    await client.post(
        "/v1/events",
        content=ndjson_body([_run_start(run_id, agent), _turn(run_id, agent),
                              _run_complete(run_id, agent)]),
        headers=_h(),
    )

    resp = await client.get("/v1/support/context")
    assert resp.status_code == 200
    data = resp.json()
    assert data["metrics"]["total_runs"] >= 1
    assert data["metrics"]["total_input_tokens"] >= 200
    assert data["metrics"]["total_cost_usd"] >= 0.0
    # The agent should appear in the agents list
    agent_names = [a["agent_name"] for a in data["agents"]]
    assert agent in agent_names


@pytest.mark.asyncio
async def test_context_shows_open_circuit_breaker(client, db):
    agent = f"ctx-cb-{uuid.uuid4().hex[:6]}"
    run_id = str(uuid.uuid4())
    await client.post(
        "/v1/events",
        content=ndjson_body([{
            "event_id": str(uuid.uuid4()),
            "event_type": "circuit_state_change",
            "run_id": run_id,
            "agent_name": agent,
            "project": "prod",
            "occurred_at": datetime.utcnow().isoformat(),
            "payload": {"resource": "anthropic", "prev_state": "closed",
                        "new_state": "open", "failure_count": 5},
        }]),
        headers=_h(),
    )

    resp = await client.get("/v1/support/context")
    data = resp.json()
    assert agent in data["circuit_breaker"]["open_agents"]
    assert len(data["circuit_breaker"]["recent_events"]) >= 1
    event = data["circuit_breaker"]["recent_events"][0]
    assert event["agent_name"] == agent
    assert event["new_state"] == "open"


@pytest.mark.asyncio
async def test_context_shows_firing_alerts(client, db, org_and_key):
    org, _ = org_and_key

    # Create a rule and a firing directly
    rule = AlertRule(
        org_id=org.id, name="test-ctx-rule",
        type="circuit_breaker_open", config={"agent_name": "*"},
        enabled=True, channel_ids=[],
    )
    db.add(rule)
    await db.flush()

    firing = AlertFiring(
        rule_id=rule.id, org_id=org.id, state="firing",
        fired_at=datetime.utcnow(), context={"agent_name": "billing-agent"},
    )
    db.add(firing)
    await db.commit()

    resp = await client.get("/v1/support/context")
    data = resp.json()
    assert data["alerts"]["firing_count"] >= 1
    assert any(f["state"] == "firing" for f in data["alerts"]["recent_firings"])


@pytest.mark.asyncio
async def test_context_audit_status(client, db, org_and_key):
    org, _ = org_and_key

    # Seed audit runs with different integrity statuses
    for integrity in ["verified", "verified", "failed", "pending"]:
        db.add(AuditRun(
            org_id=org.id, project="prod", agent_name="audit-agent",
            run_id=str(uuid.uuid4()), final_root_hash="0" * 64,
            event_count=3, integrity=integrity,
        ))
    await db.commit()

    resp = await client.get("/v1/support/context")
    data = resp.json()
    audit = data["audit"]
    assert audit["verified_runs"] >= 2
    assert audit["failed_runs"] >= 1
    assert audit["pending_runs"] >= 1
    assert audit["total_runs"] >= 4


@pytest.mark.asyncio
async def test_context_period_hours_filter(client, db, org_and_key):
    """Custom period_hours=1 should not include old metric snapshots."""
    org, _ = org_and_key

    # Old snapshot (3 hours ago)
    old_bucket = (datetime.utcnow() - timedelta(hours=3)).replace(second=0, microsecond=0)
    db.add(AgentMetricSnapshot(
        org_id=org.id, project="test", agent_name="old-agent",
        model="claude", bucket=old_bucket,
        runs_total=100, runs_success=100, runs_error=0,
        input_tokens=5000, output_tokens=2000, cost_usd=1.0,
        total_turns=100, total_duration_ms=50000,
    ))
    await db.commit()

    resp = await client.get("/v1/support/context?period_hours=1")
    data = resp.json()
    # old-agent should not appear (its bucket is 3h ago, outside 1h window)
    agent_names = [a["agent_name"] for a in data["agents"]]
    assert "old-agent" not in agent_names


@pytest.mark.asyncio
async def test_context_agents_show_cb_state(client, db, org_and_key):
    org, _ = org_and_key
    agent = f"ctx-agent-cb-{uuid.uuid4().hex[:6]}"

    # Add metric snapshot so agent appears
    db.add(AgentMetricSnapshot(
        org_id=org.id, project="prod", agent_name=agent, model="claude",
        bucket=datetime.utcnow().replace(second=0, microsecond=0),
        runs_total=10, runs_success=9, runs_error=1,
        input_tokens=1000, output_tokens=500, cost_usd=0.5,
        total_turns=10, total_duration_ms=10000,
    ))
    # Add open CB event
    db.add(CircuitBreakerEvent(
        org_id=org.id, project="prod", agent_name=agent,
        resource="anthropic", prev_state="closed", new_state="open",
        failure_count=5, occurred_at=datetime.utcnow(),
    ))
    await db.commit()

    resp = await client.get("/v1/support/context")
    data = resp.json()
    our_agent = next((a for a in data["agents"] if a["agent_name"] == agent), None)
    assert our_agent is not None
    assert our_agent["circuit_breaker_state"] == "open"
    assert our_agent["error_rate_pct"] == 10.0


@pytest.mark.asyncio
async def test_support_requires_auth():
    from httpx import ASGITransport, AsyncClient
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get("/v1/support/context")
    assert resp.status_code == 401
