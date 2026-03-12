"""Integration tests for the alerting API and evaluator pipeline."""

from __future__ import annotations

import gzip
import json
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.alerting.evaluator import (
    evaluate_all_rules,
    fire_circuit_breaker_open,
)
from app.models import AlertChannel, AlertFiring, AlertRule, AgentMetricSnapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def ndjson_body(events: list[dict]) -> bytes:
    return gzip.compress("\n".join(json.dumps(e) for e in events).encode())


def _ingest_headers() -> dict:
    return {"Content-Encoding": "gzip", "Content-Type": "application/x-ndjson"}


def _cb_event(run_id: str, prev: str, new: str, agent: str = "test-agent") -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": "circuit_state_change",
        "run_id": run_id,
        "agent_name": agent,
        "project": "prod",
        "occurred_at": datetime.utcnow().isoformat(),
        "payload": {"resource": "anthropic", "prev_state": prev, "new_state": new, "failure_count": 5},
    }


async def _make_channel(client, ch_type: str = "email", name: str | None = None) -> dict:
    config: dict = {}
    if ch_type == "email":
        config = {"to": ["ops@example.com"]}
    elif ch_type == "webhook":
        config = {"url": "https://hook.example.com/agentkit"}
    resp = await client.post(
        "/v1/alerts/channels",
        json={"name": name or f"{ch_type}-channel", "type": ch_type, "config": config},
    )
    assert resp.status_code == 201
    return resp.json()


async def _make_rule(client, rule_type: str, channel_ids: list[str],
                     config: dict | None = None, agent: str = "*") -> dict:
    body = {
        "name": f"rule-{rule_type}-{uuid.uuid4().hex[:4]}",
        "type": rule_type,
        "config": config or {"agent_name": agent},
        "channel_ids": channel_ids,
    }
    resp = await client.post("/v1/alerts/rules", json=body)
    assert resp.status_code == 201
    return resp.json()


# ---------------------------------------------------------------------------
# Channel CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_email_channel(client):
    """Email channel always returns test_sent=True (log-only, no HTTP)."""
    resp = await client.post(
        "/v1/alerts/channels",
        json={"name": "ops email", "type": "email", "config": {"to": ["ops@example.com"]}},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["test_sent"] is True
    assert data["channel"]["type"] == "email"


@pytest.mark.asyncio
async def test_list_channels(client):
    await _make_channel(client, "email", "ch1")
    await _make_channel(client, "email", "ch2")
    resp = await client.get("/v1/alerts/channels")
    assert resp.status_code == 200
    assert len(resp.json()["channels"]) >= 2


@pytest.mark.asyncio
async def test_delete_channel(client, db):
    result = await _make_channel(client, "email")
    ch_id = result["channel"]["id"]

    resp = await client.delete(f"/v1/alerts/channels/{ch_id}")
    assert resp.status_code == 204

    row = await db.execute(select(AlertChannel).where(AlertChannel.id == ch_id))
    assert row.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_create_channel_invalid_type(client):
    resp = await client.post(
        "/v1/alerts/channels",
        json={"name": "bad", "type": "sms", "config": {}},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Rule CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_list_rule(client):
    ch = await _make_channel(client, "email")
    ch_id = ch["channel"]["id"]

    rule = await _make_rule(client, "circuit_breaker_open", [ch_id], agent="billing-assistant")
    assert rule["type"] == "circuit_breaker_open"
    assert rule["enabled"] is True
    assert ch_id in rule["channel_ids"]

    resp = await client.get("/v1/alerts/rules")
    ids = [r["id"] for r in resp.json()["rules"]]
    assert rule["id"] in ids


@pytest.mark.asyncio
async def test_update_rule(client):
    ch = await _make_channel(client, "email")
    rule = await _make_rule(client, "error_rate", [ch["channel"]["id"]],
                            config={"agent_name": "*", "threshold_pct": 10.0, "window_minutes": 15, "min_runs": 5})

    resp = await client.patch(
        f"/v1/alerts/rules/{rule['id']}",
        json={"enabled": False},
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


@pytest.mark.asyncio
async def test_mute_rule(client):
    ch = await _make_channel(client, "email")
    rule = await _make_rule(client, "cost_anomaly", [ch["channel"]["id"]],
                            config={"agent_name": "*", "threshold_usd": 1.0, "window_minutes": 60, "mode": "absolute"})

    muted_until = (datetime.utcnow() + timedelta(hours=2)).isoformat()
    resp = await client.patch(
        f"/v1/alerts/rules/{rule['id']}",
        json={"muted_until": muted_until},
    )
    assert resp.status_code == 200
    assert resp.json()["muted_until"] is not None


@pytest.mark.asyncio
async def test_delete_rule_cascades_firings(client, db, org_and_key):
    org, _ = org_and_key
    ch = await _make_channel(client, "email")
    rule = await _make_rule(client, "circuit_breaker_open", [ch["channel"]["id"]])

    # Manually create a firing
    rule_row = (await db.execute(select(AlertRule).where(AlertRule.id == rule["id"]))).scalar_one()
    firing = AlertFiring(rule_id=rule_row.id, org_id=org.id, state="firing",
                         fired_at=datetime.utcnow(), context={})
    db.add(firing)
    await db.commit()

    resp = await client.delete(f"/v1/alerts/rules/{rule['id']}")
    assert resp.status_code == 204

    row = await db.execute(select(AlertRule).where(AlertRule.id == rule["id"]))
    assert row.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_create_rule_invalid_type(client):
    resp = await client.post(
        "/v1/alerts/rules",
        json={"name": "bad", "type": "unknown_type", "config": {}, "channel_ids": []},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Evaluator — circuit_breaker_open (event-driven via ingest)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cb_open_fires_alert(client, db, org_and_key):
    org, _ = org_and_key
    ch = await _make_channel(client, "email")
    agent = f"cb-fire-{uuid.uuid4().hex[:6]}"
    await _make_rule(client, "circuit_breaker_open", [ch["channel"]["id"]],
                     config={"agent_name": agent})

    run_id = str(uuid.uuid4())
    await client.post(
        "/v1/events",
        content=ndjson_body([_cb_event(run_id, "closed", "open", agent=agent)]),
        headers=_ingest_headers(),
    )

    # Verify AlertFiring was created
    rules_result = await db.execute(
        select(AlertRule).where(AlertRule.config["agent_name"].as_string() == agent)
    )
    rule = rules_result.scalar_one_or_none()
    assert rule is not None

    firing_result = await db.execute(
        select(AlertFiring).where(
            AlertFiring.rule_id == rule.id,
            AlertFiring.state == "firing",
        )
    )
    firing = firing_result.scalar_one_or_none()
    assert firing is not None
    assert firing.context["agent_name"] == agent
    assert firing.context["resource"] == "anthropic"


@pytest.mark.asyncio
async def test_cb_open_deduplication(client, db, org_and_key):
    """Two consecutive CB open events create only one firing."""
    org, _ = org_and_key
    ch = await _make_channel(client, "email")
    agent = f"cb-dedup-{uuid.uuid4().hex[:6]}"
    rule_resp = await _make_rule(client, "circuit_breaker_open", [ch["channel"]["id"]],
                                 config={"agent_name": agent})

    for _ in range(2):
        run_id = str(uuid.uuid4())
        await client.post(
            "/v1/events",
            content=ndjson_body([_cb_event(run_id, "closed", "open", agent=agent)]),
            headers=_ingest_headers(),
        )

    firings_result = await db.execute(
        select(AlertFiring).where(
            AlertFiring.rule_id == rule_resp["id"],
            AlertFiring.state == "firing",
        )
    )
    firings = list(firings_result.scalars().all())
    assert len(firings) == 1  # not 2


@pytest.mark.asyncio
async def test_cb_close_resolves_alert(client, db, org_and_key):
    """CB close event resolves an existing firing."""
    org, _ = org_and_key
    ch = await _make_channel(client, "email")
    agent = f"cb-resolve-{uuid.uuid4().hex[:6]}"
    rule_resp = await _make_rule(client, "circuit_breaker_open", [ch["channel"]["id"]],
                                 config={"agent_name": agent})

    run_id = str(uuid.uuid4())
    # Fire
    await client.post(
        "/v1/events",
        content=ndjson_body([_cb_event(run_id, "closed", "open", agent=agent)]),
        headers=_ingest_headers(),
    )
    # Resolve
    await client.post(
        "/v1/events",
        content=ndjson_body([_cb_event(run_id, "open", "closed", agent=agent)]),
        headers=_ingest_headers(),
    )

    firings_result = await db.execute(
        select(AlertFiring).where(AlertFiring.rule_id == rule_resp["id"])
    )
    firings = list(firings_result.scalars().all())
    assert any(f.state == "resolved" for f in firings)
    assert not any(f.state == "firing" for f in firings)


@pytest.mark.asyncio
async def test_muted_rule_no_firing(client, db, org_and_key):
    """A muted rule does not create a firing."""
    org, _ = org_and_key
    ch = await _make_channel(client, "email")
    agent = f"cb-muted-{uuid.uuid4().hex[:6]}"
    rule_resp = await _make_rule(client, "circuit_breaker_open", [ch["channel"]["id"]],
                                 config={"agent_name": agent})

    # Mute it
    muted_until = (datetime.utcnow() + timedelta(hours=1)).isoformat()
    await client.patch(
        f"/v1/alerts/rules/{rule_resp['id']}",
        json={"muted_until": muted_until},
    )

    run_id = str(uuid.uuid4())
    await client.post(
        "/v1/events",
        content=ndjson_body([_cb_event(run_id, "closed", "open", agent=agent)]),
        headers=_ingest_headers(),
    )

    firings_result = await db.execute(
        select(AlertFiring).where(AlertFiring.rule_id == rule_resp["id"])
    )
    assert firings_result.scalar_one_or_none() is None


# ---------------------------------------------------------------------------
# Evaluator — cost_anomaly (polled)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_anomaly_fires_when_threshold_exceeded(client, db, org_and_key):
    org, _ = org_and_key
    ch = await _make_channel(client, "email")
    agent = f"cost-alert-{uuid.uuid4().hex[:6]}"
    rule_resp = await _make_rule(
        client, "cost_anomaly", [ch["channel"]["id"]],
        config={"agent_name": agent, "mode": "absolute", "threshold_usd": 0.01, "window_minutes": 60},
    )

    # Seed a metric snapshot above threshold
    snap = AgentMetricSnapshot(
        org_id=org.id, project="test", agent_name=agent, model="claude-sonnet-4-6",
        bucket=datetime.utcnow().replace(second=0, microsecond=0),
        runs_total=5, runs_success=5, runs_error=0,
        input_tokens=1000, output_tokens=500, cost_usd=5.0,
        total_turns=10, total_duration_ms=5000,
    )
    db.add(snap)
    await db.commit()

    await evaluate_all_rules(db)
    await db.commit()

    firings_result = await db.execute(
        select(AlertFiring).where(AlertFiring.rule_id == rule_resp["id"], AlertFiring.state == "firing")
    )
    firing = firings_result.scalar_one_or_none()
    assert firing is not None
    assert firing.context["cost_usd_in_window"] >= 5.0


@pytest.mark.asyncio
async def test_cost_anomaly_no_fire_below_threshold(client, db, org_and_key):
    org, _ = org_and_key
    ch = await _make_channel(client, "email")
    agent = f"cost-ok-{uuid.uuid4().hex[:6]}"
    rule_resp = await _make_rule(
        client, "cost_anomaly", [ch["channel"]["id"]],
        config={"agent_name": agent, "mode": "absolute", "threshold_usd": 100.0, "window_minutes": 60},
    )

    snap = AgentMetricSnapshot(
        org_id=org.id, project="test", agent_name=agent, model="claude-sonnet-4-6",
        bucket=datetime.utcnow().replace(second=0, microsecond=0),
        runs_total=1, runs_success=1, runs_error=0,
        input_tokens=100, output_tokens=50, cost_usd=0.005,
        total_turns=1, total_duration_ms=1000,
    )
    db.add(snap)
    await db.commit()

    await evaluate_all_rules(db)
    await db.commit()

    firings_result = await db.execute(
        select(AlertFiring).where(AlertFiring.rule_id == rule_resp["id"])
    )
    assert firings_result.scalar_one_or_none() is None


# ---------------------------------------------------------------------------
# Evaluator — error_rate (polled)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_rate_fires(client, db, org_and_key):
    org, _ = org_and_key
    ch = await _make_channel(client, "email")
    agent = f"err-rate-{uuid.uuid4().hex[:6]}"
    rule_resp = await _make_rule(
        client, "error_rate", [ch["channel"]["id"]],
        config={"agent_name": agent, "threshold_pct": 10.0, "window_minutes": 15, "min_runs": 3},
    )

    # 8/10 runs failed = 80% error rate
    snap = AgentMetricSnapshot(
        org_id=org.id, project="test", agent_name=agent, model="claude-sonnet-4-6",
        bucket=datetime.utcnow().replace(second=0, microsecond=0),
        runs_total=10, runs_success=2, runs_error=8,
        input_tokens=1000, output_tokens=500, cost_usd=0.1,
        total_turns=10, total_duration_ms=5000,
    )
    db.add(snap)
    await db.commit()

    await evaluate_all_rules(db)
    await db.commit()

    firings_result = await db.execute(
        select(AlertFiring).where(AlertFiring.rule_id == rule_resp["id"], AlertFiring.state == "firing")
    )
    firing = firings_result.scalar_one_or_none()
    assert firing is not None
    assert firing.context["error_rate_pct"] == 80.0


@pytest.mark.asyncio
async def test_error_rate_min_runs_guard(client, db, org_and_key):
    """Does not fire when total runs < min_runs even if error rate is high."""
    org, _ = org_and_key
    ch = await _make_channel(client, "email")
    agent = f"err-min-{uuid.uuid4().hex[:6]}"
    rule_resp = await _make_rule(
        client, "error_rate", [ch["channel"]["id"]],
        config={"agent_name": agent, "threshold_pct": 10.0, "window_minutes": 15, "min_runs": 10},
    )

    snap = AgentMetricSnapshot(
        org_id=org.id, project="test", agent_name=agent, model="claude-sonnet-4-6",
        bucket=datetime.utcnow().replace(second=0, microsecond=0),
        runs_total=2, runs_success=0, runs_error=2,  # only 2 runs, need 10
        input_tokens=100, output_tokens=50, cost_usd=0.01,
        total_turns=2, total_duration_ms=1000,
    )
    db.add(snap)
    await db.commit()

    await evaluate_all_rules(db)
    await db.commit()

    firings_result = await db.execute(
        select(AlertFiring).where(AlertFiring.rule_id == rule_resp["id"])
    )
    assert firings_result.scalar_one_or_none() is None


# ---------------------------------------------------------------------------
# Firing lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ack_firing(client, db, org_and_key):
    org, _ = org_and_key
    ch = await _make_channel(client, "email")
    rule_resp = await _make_rule(
        client, "audit_integrity_failure", [ch["channel"]["id"]],
        config={"agent_name": "*", "project": "*"},
    )

    # Create firing directly
    rule_row = (await db.execute(select(AlertRule).where(AlertRule.id == rule_resp["id"]))).scalar_one()
    firing = AlertFiring(rule_id=rule_row.id, org_id=org.id, state="firing",
                         fired_at=datetime.utcnow(), context={"run_id": "abc"})
    db.add(firing)
    await db.commit()

    resp = await client.post(
        f"/v1/alerts/firing/{firing.id}/ack",
        json={"comment": "Investigated — test run, not a real failure"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "acked"
    assert "ack_comment" in data["context"]


@pytest.mark.asyncio
async def test_integrity_failure_no_auto_resolve(client, db, org_and_key):
    """audit_integrity_failure must never auto-resolve."""
    org, _ = org_and_key
    ch = await _make_channel(client, "email")
    agent = f"integrity-{uuid.uuid4().hex[:6]}"
    rule_resp = await _make_rule(
        client, "audit_integrity_failure", [ch["channel"]["id"]],
        config={"agent_name": "*", "project": "*"},
    )

    # Create a firing
    rule_row = (await db.execute(select(AlertRule).where(AlertRule.id == rule_resp["id"]))).scalar_one()
    firing = AlertFiring(rule_id=rule_row.id, org_id=org.id, state="firing",
                         fired_at=datetime.utcnow(), context={})
    db.add(firing)
    await db.commit()

    # Run evaluator — should NOT resolve audit_integrity_failure
    await evaluate_all_rules(db)
    await db.commit()

    await db.refresh(firing)
    assert firing.state == "firing"  # still firing


@pytest.mark.asyncio
async def test_list_firings_filter_by_state(client, db, org_and_key):
    org, _ = org_and_key
    ch = await _make_channel(client, "email")
    rule_resp = await _make_rule(client, "circuit_breaker_open", [ch["channel"]["id"]])

    rule_row = (await db.execute(select(AlertRule).where(AlertRule.id == rule_resp["id"]))).scalar_one()
    f1 = AlertFiring(rule_id=rule_row.id, org_id=org.id, state="firing",
                     fired_at=datetime.utcnow(), context={})
    f2 = AlertFiring(rule_id=rule_row.id, org_id=org.id, state="resolved",
                     fired_at=datetime.utcnow() - timedelta(hours=1),
                     resolved_at=datetime.utcnow(), context={})
    db.add_all([f1, f2])
    await db.commit()

    resp = await client.get("/v1/alerts/firing?state=firing")
    data = resp.json()
    assert all(f["state"] == "firing" for f in data["firings"])

    resp2 = await client.get("/v1/alerts/firing?state=resolved")
    assert all(f["state"] == "resolved" for f in resp2.json()["firings"])


# ---------------------------------------------------------------------------
# Webhook dispatch — HMAC signature
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_hmac_signature(client, db, org_and_key):
    """Webhook channel includes correct HMAC signature when secret is set."""
    import hashlib
    import hmac as _hmac
    import json as _json
    from app.alerting.dispatch import _send_webhook

    config = {"url": "https://hook.example.com/test", "secret": "my-secret"}
    captured: list[dict] = []

    async def _fake_post(url: str, content: bytes, headers: dict) -> None:
        captured.append({"url": url, "body": content, "headers": headers})

    # Patch the httpx.AsyncClient.post
    with patch("httpx.AsyncClient") as mock_client_cls:
        instance = AsyncMock()
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        mock_response = AsyncMock()
        mock_response.raise_for_status = AsyncMock()
        instance.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = instance

        payload = {"event": "alert.firing", "rule_name": "test", "alert_id": "abc"}
        await _send_webhook(config, "alert.firing", payload)

        # Verify post was called
        assert instance.post.called
        call_kwargs = instance.post.call_args
        body_bytes = call_kwargs.kwargs.get("content") or call_kwargs.args[1]
        headers_sent = call_kwargs.kwargs.get("headers") or call_kwargs.args[2]

        expected_sig = _hmac.new(
            b"my-secret", body_bytes, hashlib.sha256
        ).hexdigest()
        assert headers_sent["X-AgentKit-Signature"] == f"sha256={expected_sig}"


@pytest.mark.asyncio
async def test_alerts_require_auth():
    from httpx import ASGITransport, AsyncClient
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get("/v1/alerts/rules")
    assert resp.status_code == 401
