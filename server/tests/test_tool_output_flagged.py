"""tool_output_flagged: SDK scanner findings reach the alert evaluator."""

from __future__ import annotations

import gzip
import json
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import AlertFiring, CloudEventLog


def ndjson_body(events: list[dict]) -> bytes:
    return gzip.compress("\n".join(json.dumps(e) for e in events).encode())


HEADERS = {"Content-Encoding": "gzip", "Content-Type": "application/x-ndjson"}


def flagged_event(agent: str, severity: str = "high", project: str = "prod", run_id: str | None = None) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": "tool_output_flagged",
        "run_id": run_id or str(uuid.uuid4()),
        "agent_name": agent,
        "project": project,
        "occurred_at": datetime.utcnow().isoformat(),
        "payload": {
            "call_id": "call-1",
            "tool_name": "fetch_page",
            "action": "blocked",
            "max_severity": severity,
            "findings": [
                {"scanner": "patterns", "rule": "unicode_tags", "severity": severity, "location": "$.body", "indicator": None},
                {"scanner": "nullcone", "rule": "ioc_domain", "severity": "medium", "location": "$.body", "indicator": "evil-login.net"},
            ],
        },
    }


async def make_channel(client):
    return await client.post("/v1/alerts/channels", json={"name": "ops", "type": "email",
                                                            "config": {"to": ["ops@acme.test"]}})


async def make_rule(client, config: dict) -> dict:
    channel = await make_channel(client)
    assert channel.status_code == 201
    resp = await client.post("/v1/alerts/rules", json={
        "name": f"flagged-{uuid.uuid4().hex[:4]}", "type": "tool_output_flagged", "config": config,
        "channel_ids": [channel.json()["channel"]["id"]],
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


async def send(client, *events: dict) -> None:
    resp = await client.post("/v1/events", content=ndjson_body(list(events)), headers=HEADERS)
    assert resp.status_code == 202


async def firings(db, rule_id: str) -> list[AlertFiring]:
    result = await db.execute(select(AlertFiring).where(AlertFiring.rule_id == rule_id))
    return list(result.scalars().all())


@pytest.fixture(autouse=True)
def no_dispatch(monkeypatch):
    async def noop(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr("app.alerting.dispatch.dispatch_alert", noop)


async def test_fires_at_or_above_min_severity_with_context(client, db):
    agent = f"scan-{uuid.uuid4().hex[:6]}"
    rule = await make_rule(client, {"agent_name": agent, "min_severity": "high"})
    run_id = str(uuid.uuid4())

    await send(client, flagged_event(agent, severity="critical", run_id=run_id))

    (firing,) = await firings(db, rule["id"])
    assert firing.state == "firing"
    assert firing.context == {
        "run_id": run_id, "agent_name": agent, "project": "prod", "tool_name": "fetch_page", "action": "blocked",
        "max_severity": "critical", "rules": ["ioc_domain", "unicode_tags"], "indicators": ["evil-login.net"],
    }


async def test_below_min_severity_does_not_fire(client, db):
    agent = f"scan-{uuid.uuid4().hex[:6]}"
    default_rule = await make_rule(client, {"agent_name": agent})
    strict_rule = await make_rule(client, {"agent_name": agent, "min_severity": "critical"})
    await send(client, flagged_event(agent, severity="medium"), flagged_event(agent, severity="high"))
    assert len(await firings(db, default_rule["id"])) == 1  # default min_severity is high
    assert await firings(db, strict_rule["id"]) == []


async def test_agent_and_project_filters_and_unknown_severity(client, db):
    agent = f"scan-{uuid.uuid4().hex[:6]}"
    rule = await make_rule(client, {"agent_name": agent, "project": "prod"})
    await send(client, flagged_event("someone-else"), flagged_event(agent, project="staging"),
               flagged_event(agent, severity="catastrophic"))
    assert await firings(db, rule["id"]) == []
    wildcard = await make_rule(client, {"agent_name": "*", "project": "*"})
    await send(client, flagged_event(agent, project="staging"))
    assert len(await firings(db, wildcard["id"])) == 1


async def test_muted_rule_and_deduplication(client, db):
    agent = f"scan-{uuid.uuid4().hex[:6]}"
    muted = await make_rule(client, {"agent_name": agent})
    await client.patch(f"/v1/alerts/rules/{muted['id']}",
                       json={"muted_until": (datetime.utcnow() + timedelta(hours=1)).isoformat()})
    active = await make_rule(client, {"agent_name": agent})
    await send(client, flagged_event(agent), flagged_event(agent))
    assert await firings(db, muted["id"]) == []
    assert len(await firings(db, active["id"])) == 1


async def test_rule_validation_rejects_bad_min_severity(client):
    channel = await make_channel(client)
    body = {"name": "bad", "type": "tool_output_flagged", "config": {"min_severity": "severe"},
            "channel_ids": [channel.json()["channel"]["id"]]}
    assert (await client.post("/v1/alerts/rules", json=body)).status_code == 400
    rule = await make_rule(client, {"min_severity": "low"})
    resp = await client.patch(f"/v1/alerts/rules/{rule['id']}", json={"config": {"min_severity": "extreme"}})
    assert resp.status_code == 400


async def test_event_is_stored(client, db):
    agent = f"scan-{uuid.uuid4().hex[:6]}"
    event = flagged_event(agent)
    await send(client, event)
    result = await db.execute(select(CloudEventLog).where(CloudEventLog.event_id == event["event_id"]))
    stored = result.scalar_one()
    assert (stored.event_type, stored.payload["max_severity"]) == ("tool_output_flagged", "high")
