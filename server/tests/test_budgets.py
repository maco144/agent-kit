"""Fleet budgets: periods, spend, trip/close, alerts, API."""

from __future__ import annotations

import gzip
import json
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.budgets import evaluate_budgets, period_end, period_start
from app.models import ActiveRunCache, AgentMetricSnapshot, AlertFiring, AlertRule, Budget


NOW = datetime(2026, 9, 16, 15, 30)  # a Wednesday


@pytest.mark.parametrize(("period", "start", "end"), [
    ("daily", datetime(2026, 9, 16), datetime(2026, 9, 17)),
    ("weekly", datetime(2026, 9, 14), datetime(2026, 9, 21)),
    ("monthly", datetime(2026, 9, 1), datetime(2026, 10, 1)),
])
def test_calendar_periods(period, start, end):
    assert period_start(period, NOW) == start
    assert period_end(period, start) == end


def test_monthly_period_rolls_over_year():
    start = period_start("monthly", datetime(2026, 12, 31, 23, 59))
    assert (start, period_end("monthly", start)) == (datetime(2026, 12, 1), datetime(2027, 1, 1))


def snapshot(org_id, cost, bucket, project="prod", agent="support"):
    return AgentMetricSnapshot(org_id=org_id, project=project, agent_name=agent, model="m", bucket=bucket,
                               runs_total=1, runs_success=1, runs_error=0, input_tokens=0, output_tokens=0,
                               cost_usd=cost, total_turns=1, total_duration_ms=1)


def active(org_id, cost, started_at, project="prod", agent="support", last_event_at=None):
    return ActiveRunCache(run_id=str(uuid.uuid4()), org_id=org_id, project=project, agent_name=agent,
                          model="m", started_at=started_at, cost_so_far_usd=cost, last_event_at=last_event_at)


async def make_budget(db, org_id, **fields):
    budget = Budget(org_id=org_id, name=fields.pop("name", "support daily"), period=fields.pop("period", "daily"),
                    limit_usd=fields.pop("limit_usd", 10.0), **fields)
    db.add(budget)
    await db.commit()
    return budget


async def test_spend_counts_period_snapshots_and_in_flight_runs(db, org_and_key):
    org, _ = org_and_key
    budget = await make_budget(db, org.id, project="prod", agent_name="*", limit_usd=10.0)
    db.add_all([
        snapshot(org.id, 3.0, NOW.replace(hour=1)),                    # counts
        snapshot(org.id, 2.0, NOW - timedelta(days=1)),                # previous period
        snapshot(org.id, 4.0, NOW.replace(hour=2), project="staging"), # other project
        active(org.id, 1.5, NOW - timedelta(minutes=10)),              # in flight
        active(org.id, 9.0, NOW - timedelta(hours=3)),                 # stale, no activity
        active(org.id, 0.5, NOW - timedelta(hours=3), last_event_at=NOW - timedelta(minutes=5)),  # long but active
    ])
    other_org = snapshot(str(uuid.uuid4()), 100.0, NOW.replace(hour=1))
    db.add(other_org)
    await db.commit()

    (status,) = await evaluate_budgets(org.id, db, NOW)

    assert status.budget.id == budget.id
    assert status.spent_usd == pytest.approx(5.0)
    assert status.remaining_usd == pytest.approx(5.0)
    assert status.tripped is False
    assert (status.period_start, status.resets_at) == (datetime(2026, 9, 16), datetime(2026, 9, 17))


async def test_trip_sets_tripped_at_and_fires_matching_alerts(db, org_and_key):
    org, _ = org_and_key
    budget = await make_budget(db, org.id, agent_name="support", limit_usd=5.0)
    specific = AlertRule(org_id=org.id, name="support budget", type="budget_exceeded",
                         config={"budget_id": budget.id}, channel_ids=[])
    wildcard = AlertRule(org_id=org.id, name="any budget", type="budget_exceeded",
                         config={"budget_id": "*"}, channel_ids=[])
    unrelated = AlertRule(org_id=org.id, name="other budget", type="budget_exceeded",
                          config={"budget_id": str(uuid.uuid4())}, channel_ids=[])
    db.add_all([specific, wildcard, unrelated, snapshot(org.id, 6.0, NOW.replace(hour=9))])
    await db.commit()

    (status,) = await evaluate_budgets(org.id, db, NOW)
    await db.commit()
    await evaluate_budgets(org.id, db, NOW)  # already tripped: no duplicate firings
    await db.commit()

    assert status.tripped is True
    await db.refresh(budget)
    assert budget.tripped_at == NOW
    firings = (await db.execute(select(AlertFiring).where(AlertFiring.org_id == org.id))).scalars().all()
    assert sorted(f.rule_id for f in firings) == sorted([specific.id, wildcard.id])
    context = next(f.context for f in firings if f.rule_id == specific.id)
    assert context["budget_name"] == "support daily"
    assert context["limit_usd"] == 5.0
    assert context["spent_usd"] == pytest.approx(6.0)
    assert context["resets_at"] == "2026-09-17T00:00:00"


async def test_period_rollover_closes_and_resolves(db, org_and_key):
    org, _ = org_and_key
    budget = await make_budget(db, org.id, limit_usd=5.0)
    rule = AlertRule(org_id=org.id, name="r", type="budget_exceeded", config={"budget_id": "*"}, channel_ids=[])
    db.add_all([rule, snapshot(org.id, 6.0, NOW.replace(hour=9))])
    await db.commit()
    await evaluate_budgets(org.id, db, NOW)
    await db.commit()

    (status,) = await evaluate_budgets(org.id, db, NOW + timedelta(days=1))
    await db.commit()

    assert status.tripped is False
    await db.refresh(budget)
    assert budget.tripped_at is None
    firing = (await db.execute(select(AlertFiring).where(AlertFiring.rule_id == rule.id))).scalar_one()
    assert firing.state == "resolved"


async def test_raising_the_limit_or_disabling_closes(db, org_and_key):
    org, _ = org_and_key
    raised = await make_budget(db, org.id, name="raised", limit_usd=5.0)
    disabled = await make_budget(db, org.id, name="disabled", limit_usd=5.0)
    db.add(snapshot(org.id, 6.0, NOW.replace(hour=9)))
    await db.commit()
    await evaluate_budgets(org.id, db, NOW)
    await db.commit()

    raised.limit_usd = 50.0
    disabled.enabled = False
    await db.commit()
    statuses = {s.budget.name: s for s in await evaluate_budgets(org.id, db, NOW)}
    await db.commit()

    assert statuses["raised"].tripped is False and statuses["disabled"].tripped is False
    await db.refresh(raised)
    await db.refresh(disabled)
    assert raised.tripped_at is None and disabled.tripped_at is None


async def test_budget_exceeded_is_a_valid_alert_rule_type(client):
    resp = await client.post("/v1/alerts/rules", json={
        "name": "budgets", "type": "budget_exceeded", "config": {"budget_id": "*"}, "channel_ids": [],
    })
    assert resp.status_code == 201


async def test_budget_crud_and_validation(client):
    created = await client.post("/v1/budgets", json={"name": "support", "period": "daily", "limit_usd": 25,
                                                      "project": "prod", "agent_name": "support"})
    assert created.status_code == 201
    budget = created.json()
    assert (budget["limit_usd"], budget["spent_usd"], budget["tripped"]) == (25.0, 0.0, False)
    assert budget["resets_at"] > budget["period_start"]

    assert (await client.post("/v1/budgets", json={"name": "x", "period": "hourly", "limit_usd": 1})).status_code == 400
    assert (await client.post("/v1/budgets", json={"name": "x", "period": "daily", "limit_usd": 0})).status_code == 400

    listed = (await client.get("/v1/budgets")).json()["budgets"]
    assert [b["id"] for b in listed] == [budget["id"]]

    patched = await client.patch(f"/v1/budgets/{budget['id']}", json={"limit_usd": 40, "enabled": False})
    assert (patched.json()["limit_usd"], patched.json()["enabled"]) == (40.0, False)
    assert (await client.patch(f"/v1/budgets/{budget['id']}", json={"period": "yearly"})).status_code == 400

    assert (await client.delete(f"/v1/budgets/{budget['id']}")).status_code == 204
    assert (await client.get("/v1/budgets")).json()["budgets"] == []
    assert (await client.delete(f"/v1/budgets/{budget['id']}")).status_code == 404


async def test_status_endpoint_scopes_to_agent(client):
    for body in [
        {"name": "org", "period": "monthly", "limit_usd": 500},
        {"name": "support", "period": "daily", "limit_usd": 20, "agent_name": "support"},
        {"name": "billing", "period": "daily", "limit_usd": 20, "agent_name": "billing"},
        {"name": "staging", "period": "daily", "limit_usd": 20, "project": "staging"},
        {"name": "off", "period": "daily", "limit_usd": 20, "enabled": False},
    ]:
        assert (await client.post("/v1/budgets", json=body)).status_code == 201

    resp = await client.get("/v1/budgets/status", params={"project": "prod", "agent_name": "support"})

    assert sorted(b["name"] for b in resp.json()["budgets"]) == ["org", "support"]


def _events(run_id: str, cost: float) -> bytes:
    now = datetime.utcnow().isoformat()
    base = {"run_id": run_id, "agent_name": "support", "project": "prod", "occurred_at": now}
    events = [
        {**base, "event_id": str(uuid.uuid4()), "event_type": "run_start", "payload": {"model": "claude-opus-5"}},
        {**base, "event_id": str(uuid.uuid4()), "event_type": "turn_complete",
         "payload": {"input_tokens": 1, "output_tokens": 1, "cost_usd": cost}},
    ]
    return gzip.compress("\n".join(json.dumps(e) for e in events).encode())


async def test_ingest_trips_budget_and_patch_closes_it(client, db, org_and_key):
    org, _ = org_and_key
    budget = (await client.post("/v1/budgets", json={"name": "support", "period": "daily",
                                                      "limit_usd": 1.0, "agent_name": "support"})).json()
    rule = (await client.post("/v1/alerts/rules", json={"name": "b", "type": "budget_exceeded",
                                                         "config": {"budget_id": budget["id"]}, "channel_ids": []})).json()

    resp = await client.post("/v1/events", content=_events(str(uuid.uuid4()), 1.25),
                             headers={"Content-Encoding": "gzip", "Content-Type": "application/x-ndjson"})
    assert resp.status_code == 202

    status = (await client.get("/v1/budgets/status", params={"project": "prod", "agent_name": "support"})).json()
    assert status["budgets"][0]["tripped"] is True
    assert status["budgets"][0]["spent_usd"] == pytest.approx(1.25)
    firing = (await db.execute(select(AlertFiring).where(AlertFiring.rule_id == rule["id"]))).scalar_one()
    assert firing.state == "firing"

    patched = (await client.patch(f"/v1/budgets/{budget['id']}", json={"limit_usd": 5.0})).json()
    assert patched["tripped"] is False
    await db.refresh(firing)
    assert firing.state == "resolved"


async def test_delete_resolves_active_alerts(client, db):
    budget = (await client.post("/v1/budgets", json={"name": "b", "period": "daily", "limit_usd": 0.5,
                                                      "agent_name": "support"})).json()
    rule = (await client.post("/v1/alerts/rules", json={"name": "r", "type": "budget_exceeded",
                                                         "config": {"budget_id": budget["id"]}, "channel_ids": []})).json()
    await client.post("/v1/events", content=_events(str(uuid.uuid4()), 1.0),
                      headers={"Content-Encoding": "gzip", "Content-Type": "application/x-ndjson"})

    await client.delete(f"/v1/budgets/{budget['id']}")

    firing = (await db.execute(select(AlertFiring).where(AlertFiring.rule_id == rule["id"]))).scalar_one()
    assert firing.state == "resolved"


async def test_budgets_require_auth():
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        assert (await anon.get("/v1/budgets")).status_code == 401
