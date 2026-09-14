# Cost Circuit Breaker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce dollar ceilings on agent spend — a local per-run cap and server-defined calendar-period fleet budgets — with `budget_exceeded` alerts, across native agent-kit agents and (opt-in) Claude Agent SDK / OpenAI Agents SDK agents.

**Architecture:** The server stores budgets, computes period spend from metric snapshots plus in-flight runs, reconciles `tripped_at` after ingest / reads / edits / the worker, and fires or resolves `budget_exceeded` alerts on transitions. SDKs fetch `/v1/budgets/status` through a shared `BudgetGuard` (cached, fail-open) and raise `BudgetExceededError` before model calls; the per-run cap is checked in `AgentLoop`. Adapters call the same guard from Claude hooks and a new OpenAI `RunHooks`.

**Tech Stack:** FastAPI, SQLAlchemy 2 async, Alembic; agent-kit SDK (httpx), pytest + pytest-asyncio (auto), `claude-agent-sdk`, `openai-agents`.

**Spec:** `specs/09-cost-circuit-breaker.md`

## Global Constraints

- Server never imports the SDK; SDK never imports the server.
- Periods are UTC calendar periods: daily (00:00), weekly (Monday 00:00), monthly (1st 00:00).
- Scope wildcard: `"*"` matches anything, otherwise exact match.
- Budget evaluation errors never fail ingest; guard fetch errors never raise unless `fail_closed=True`.
- Alerts fire and resolve only on trip/close transitions (acked firings don't re-fire on every evaluation).
- SDK: `ruff check agent_kit tests`, `mypy agent_kit`, `pytest` clean with and without the harness extras. Server: `ruff check app tests`, `pytest`, `alembic upgrade head` clean.
- No test touches the network (httpx `MockTransport` for the guard; in-process app for the server).

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `server/migrations/versions/006_budgets.py` | `budgets` table | Create |
| `server/app/models.py` | `Budget` | Modify |
| `server/app/schemas.py` | Budget request/response schemas | Modify |
| `server/app/budgets.py` | Periods, spend, status, evaluation + alert reconciliation | Create |
| `server/app/routers/budgets.py` | CRUD + status | Create |
| `server/app/routers/alerts.py` | `budget_exceeded` rule type | Modify |
| `server/app/routers/ingest.py`, `server/app/routers/otlp.py` | Evaluate budgets after commit | Modify |
| `server/app/main.py` | Router + worker evaluation | Modify |
| `server/tests/test_budgets.py` | Server behaviour | Create |
| `agent_kit/exceptions.py` | `BudgetExceededError` | Modify |
| `agent_kit/cloud/budgets.py` | `BudgetGuard` | Create |
| `agent_kit/cloud/reporter.py` | `budget_guard()`, public `api_key`/`base_url` | Modify |
| `agent_kit/agent/agent.py`, `agent_kit/agent/loop.py` | `max_run_cost_usd`, `enforce_budgets` | Modify |
| `agent_kit/integrations/recorder.py` | `llm_turn` returns priced cost; public `price_call` | Modify |
| `agent_kit/integrations/claude_agent_sdk.py` | Enforcement in hooks; `max_budget_usd` | Modify |
| `agent_kit/integrations/openai_agents.py` | `AgentKitRunHooks` | Modify |
| `tests/test_budgets.py`, `tests/test_integrations_claude.py`, `tests/test_integrations_openai_agents.py` | SDK + adapter tests | Create / Modify |
| `docs/api-reference.md`, `docs/cloud-quickstart.md`, `README.md`, `CHANGELOG.md`, `specs/06-harness-roadmap.md`, `specs/09-cost-circuit-breaker.md`, `PROJECT_INDEX.md` | Docs | Modify |

---

### Task 1: Budgets on the server — model, periods, spend, evaluation

**Files:**
- Create: `server/migrations/versions/006_budgets.py`, `server/app/budgets.py`, `server/tests/test_budgets.py`
- Modify: `server/app/models.py`, `server/app/routers/alerts.py`

**Interfaces:**
- Produces: `Budget` model; `budgets.period_start(period: str, now: datetime) -> datetime`, `budgets.period_end(period, start) -> datetime`, `BudgetStatus(budget, spent_usd, remaining_usd, tripped, period_start, resets_at)`, `async budget_status(budget, db, now) -> BudgetStatus`, `async evaluate_budgets(org_id, db, now=None) -> list[BudgetStatus]`, `async resolve_budget_alerts(org_id, budget_id, db) -> None`, `async evaluate_all_budgets(db, now=None) -> None`, `budgets.matches(budget, project, agent_name) -> bool`, `PERIODS`.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_budgets.py
"""Fleet budgets: periods, spend, trip/close, alerts, API."""

from __future__ import annotations

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && pytest tests/test_budgets.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.budgets'`

- [ ] **Step 3: Implement**

`server/app/models.py` — after `AlertFiring`:

```python
# ---------------------------------------------------------------------------
# Budgets (cost circuit breaker)
# ---------------------------------------------------------------------------


class Budget(Base):
    """A spend ceiling for matching agents over a UTC calendar period."""
    __tablename__ = "budgets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    project: Mapped[str] = mapped_column(String(255), nullable=False, default="*")
    agent_name: Mapped[str] = mapped_column(String(255), nullable=False, default="*")
    period: Mapped[str] = mapped_column(String(16), nullable=False)  # daily | weekly | monthly
    limit_usd: Mapped[float] = mapped_column(Float, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    tripped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now, nullable=False)

    __table_args__ = (
        Index("ix_budgets_org_enabled", "org_id", "enabled"),
    )
```

```python
# server/migrations/versions/006_budgets.py
"""Budgets for the cost circuit breaker.

Revision ID: 006
Revises: 005
Create Date: 2026-09-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "budgets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("project", sa.String(255), nullable=False, server_default="*"),
        sa.Column("agent_name", sa.String(255), nullable=False, server_default="*"),
        sa.Column("period", sa.String(16), nullable=False),
        sa.Column("limit_usd", sa.Float, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("tripped_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_budgets_org_enabled", "budgets", ["org_id", "enabled"])


def downgrade() -> None:
    op.drop_index("ix_budgets_org_enabled", table_name="budgets")
    op.drop_table("budgets")
```

`server/app/routers/alerts.py`:

```python
_VALID_RULE_TYPES = {
    "circuit_breaker_open", "cost_anomaly", "error_rate", "audit_integrity_failure", "budget_exceeded",
}
```

```python
# server/app/budgets.py
"""
Fleet budgets: calendar-period spend ceilings for matching agents.

Spend = metric snapshots in the current UTC period + cost of in-flight runs, so a
runaway run counts before it finishes. evaluate_budgets() reconciles each budget's
tripped state and fires / resolves budget_exceeded alerts on transitions only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ActiveRunCache, AgentMetricSnapshot, AlertRule, Budget

logger = logging.getLogger("agentkit.cloud.budgets")

PERIODS = ("daily", "weekly", "monthly")
_ACTIVE_WINDOW = timedelta(hours=1)


def period_start(period: str, now: datetime) -> datetime:
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "daily":
        return day
    if period == "weekly":
        return day - timedelta(days=day.weekday())
    if period == "monthly":
        return day.replace(day=1)
    raise ValueError(f"unknown period {period!r}")


def period_end(period: str, start: datetime) -> datetime:
    if period == "daily":
        return start + timedelta(days=1)
    if period == "weekly":
        return start + timedelta(days=7)
    if period == "monthly":
        return start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    raise ValueError(f"unknown period {period!r}")


def matches(budget: Budget, project: str, agent_name: str) -> bool:
    return budget.project in ("*", project) and budget.agent_name in ("*", agent_name)


@dataclass
class BudgetStatus:
    budget: Budget
    spent_usd: float
    remaining_usd: float
    tripped: bool
    period_start: datetime
    resets_at: datetime


async def budget_status(budget: Budget, db: AsyncSession, now: datetime) -> BudgetStatus:
    start = period_start(budget.period, now)

    snapshots = select(func.coalesce(func.sum(AgentMetricSnapshot.cost_usd), 0.0)).where(
        AgentMetricSnapshot.org_id == budget.org_id,
        AgentMetricSnapshot.bucket >= start,
    )
    in_flight = select(func.coalesce(func.sum(ActiveRunCache.cost_so_far_usd), 0.0)).where(
        ActiveRunCache.org_id == budget.org_id,
        or_(
            ActiveRunCache.last_event_at >= now - _ACTIVE_WINDOW,
            and_(ActiveRunCache.last_event_at.is_(None), ActiveRunCache.started_at >= now - _ACTIVE_WINDOW),
        ),
    )
    if budget.project != "*":
        snapshots = snapshots.where(AgentMetricSnapshot.project == budget.project)
        in_flight = in_flight.where(ActiveRunCache.project == budget.project)
    if budget.agent_name != "*":
        snapshots = snapshots.where(AgentMetricSnapshot.agent_name == budget.agent_name)
        in_flight = in_flight.where(ActiveRunCache.agent_name == budget.agent_name)

    spent = float((await db.execute(snapshots)).scalar_one()) + float((await db.execute(in_flight)).scalar_one())
    return BudgetStatus(
        budget=budget,
        spent_usd=spent,
        remaining_usd=max(0.0, budget.limit_usd - spent),
        tripped=bool(budget.enabled) and spent >= budget.limit_usd,
        period_start=start,
        resets_at=period_end(budget.period, start),
    )


async def evaluate_budgets(
    org_id: str, db: AsyncSession, now: datetime | None = None
) -> list[BudgetStatus]:
    """Compute live status for every budget in the org and reconcile trip state + alerts."""
    current = now or datetime.utcnow()
    budgets = (await db.execute(select(Budget).where(Budget.org_id == org_id).order_by(Budget.created_at))).scalars().all()
    statuses = [await budget_status(b, db, current) for b in budgets]

    for status in statuses:
        budget = status.budget
        stale_trip = budget.tripped_at is not None and budget.tripped_at < status.period_start
        if status.tripped and (budget.tripped_at is None or stale_trip):
            budget.tripped_at = current
            await _fire(status, db)
        elif not status.tripped and budget.tripped_at is not None:
            budget.tripped_at = None
            await resolve_budget_alerts(org_id, budget.id, db, any_tripped=any(s.tripped for s in statuses))
    return statuses


async def resolve_budget_alerts(
    org_id: str, budget_id: str, db: AsyncSession, any_tripped: bool = False
) -> None:
    """Resolve alerts for one budget; wildcard rules resolve only when no budget is tripped."""
    from app.alerting.evaluator import _get_active_firing, _resolve_firing

    for rule in await _rules(org_id, db):
        target = rule.config.get("budget_id")
        if target == budget_id or (target == "*" and not any_tripped):
            firing = await _get_active_firing(rule.id, db)
            if firing is not None:
                await _resolve_firing(rule, firing, db)


async def evaluate_all_budgets(db: AsyncSession, now: datetime | None = None) -> None:
    """Background worker entry point: every org with a budget."""
    org_ids = (await db.execute(select(Budget.org_id).distinct())).scalars().all()
    for org_id in org_ids:
        try:
            await evaluate_budgets(org_id, db, now)
        except Exception as exc:
            logger.warning("Budget evaluation failed for org %s: %s", org_id, exc)


async def _fire(status: BudgetStatus, db: AsyncSession) -> None:
    from app.alerting.evaluator import _create_firing

    budget = status.budget
    now = datetime.utcnow()
    context = {
        "budget_id": budget.id,
        "budget_name": budget.name,
        "project": budget.project,
        "agent_name": budget.agent_name,
        "period": budget.period,
        "limit_usd": budget.limit_usd,
        "spent_usd": round(status.spent_usd, 6),
        "resets_at": status.resets_at.isoformat(),
        "type": "budget_exceeded",
    }
    for rule in await _rules(budget.org_id, db):
        if rule.config.get("budget_id") in (budget.id, "*"):
            if rule.muted_until and rule.muted_until > now:
                continue
            await _create_firing(rule, context, db)


async def _rules(org_id: str, db: AsyncSession) -> list[AlertRule]:
    result = await db.execute(
        select(AlertRule).where(
            AlertRule.org_id == org_id,
            AlertRule.type == "budget_exceeded",
            AlertRule.enabled == True,  # noqa: E712
        )
    )
    return list(result.scalars().all())
```

- [ ] **Step 4: Run tests and migration**

Run: `cd server && pytest tests/test_budgets.py -v && pytest && ruff check app tests && DATABASE_URL=sqlite+aiosqlite:////tmp/b006.db alembic upgrade head`
Expected: PASS; `005 -> 006`.

- [ ] **Step 5: Commit**

```bash
git add server/app/models.py server/app/budgets.py server/app/routers/alerts.py server/migrations/versions/006_budgets.py server/tests/test_budgets.py
git commit -m "feat(server): budgets with calendar-period spend and trip reconciliation"
```

---

### Task 2: Budgets API and evaluation triggers

**Files:**
- Create: `server/app/routers/budgets.py`
- Modify: `server/app/schemas.py`, `server/app/main.py`, `server/app/routers/ingest.py`, `server/app/routers/otlp.py`, `server/tests/test_budgets.py`

**Interfaces:**
- Consumes: Task 1.
- Produces: `GET/POST /v1/budgets`, `PATCH/DELETE /v1/budgets/{id}`, `GET /v1/budgets/status?project=&agent_name=` → `{"budgets": [BudgetOut]}`; `BudgetOut` fields `id, name, project, agent_name, period, limit_usd, enabled, spent_usd, remaining_usd, tripped, tripped_at, period_start, resets_at`.

- [ ] **Step 1: Write the failing tests**

```python
# append to server/tests/test_budgets.py
import gzip
import json


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && pytest tests/test_budgets.py -v`
Expected: new tests FAIL with `404` (no router).

- [ ] **Step 3: Implement**

`server/app/schemas.py` — append:

```python
# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


class CreateBudgetRequest(BaseModel):
    name: str
    period: str
    limit_usd: float
    project: str = "*"
    agent_name: str = "*"
    enabled: bool = True


class UpdateBudgetRequest(BaseModel):
    name: str | None = None
    period: str | None = None
    limit_usd: float | None = None
    project: str | None = None
    agent_name: str | None = None
    enabled: bool | None = None


class BudgetOut(BaseModel):
    id: str
    name: str
    project: str
    agent_name: str
    period: str
    limit_usd: float
    enabled: bool
    spent_usd: float
    remaining_usd: float
    tripped: bool
    tripped_at: datetime | None
    period_start: datetime
    resets_at: datetime


class BudgetList(BaseModel):
    budgets: list[BudgetOut]
```

```python
# server/app/routers/budgets.py
"""Fleet budgets — the server side of the cost circuit breaker."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_org
from app.budgets import PERIODS, BudgetStatus, evaluate_budgets, matches, resolve_budget_alerts
from app.database import get_db
from app.models import Budget, Organization
from app.schemas import BudgetList, BudgetOut, CreateBudgetRequest, UpdateBudgetRequest

router = APIRouter(prefix="/v1/budgets", tags=["budgets"])


def _out(s: BudgetStatus) -> BudgetOut:
    b = s.budget
    return BudgetOut(
        id=b.id, name=b.name, project=b.project, agent_name=b.agent_name, period=b.period,
        limit_usd=b.limit_usd, enabled=b.enabled, spent_usd=round(s.spent_usd, 6),
        remaining_usd=round(s.remaining_usd, 6), tripped=s.tripped, tripped_at=b.tripped_at,
        period_start=s.period_start, resets_at=s.resets_at,
    )


def _validate(period: str | None, limit_usd: float | None) -> None:
    if period is not None and period not in PERIODS:
        raise HTTPException(status_code=400, detail=f"period must be one of {list(PERIODS)}")
    if limit_usd is not None and limit_usd <= 0:
        raise HTTPException(status_code=400, detail="limit_usd must be greater than 0")


async def _evaluate(org_id: str, db: AsyncSession) -> list[BudgetStatus]:
    statuses = await evaluate_budgets(org_id, db, datetime.utcnow())
    await db.commit()
    return statuses


async def _get_budget(budget_id: str, org_id: str, db: AsyncSession) -> Budget:
    budget = (
        await db.execute(select(Budget).where(Budget.id == budget_id, Budget.org_id == org_id))
    ).scalar_one_or_none()
    if budget is None:
        raise HTTPException(status_code=404, detail="Budget not found")
    return budget


@router.get("", response_model=BudgetList)
async def list_budgets(
    org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db)
) -> BudgetList:
    return BudgetList(budgets=[_out(s) for s in await _evaluate(org.id, db)])


@router.get("/status", response_model=BudgetList)
async def budget_status_for_agent(
    project: str = Query(""),
    agent_name: str = Query(""),
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> BudgetList:
    """Enabled budgets that apply to one agent. SDKs poll this to enforce fleet budgets."""
    statuses = await _evaluate(org.id, db)
    return BudgetList(
        budgets=[_out(s) for s in statuses if s.budget.enabled and matches(s.budget, project, agent_name)]
    )


@router.post("", response_model=BudgetOut, status_code=status.HTTP_201_CREATED)
async def create_budget(
    body: CreateBudgetRequest,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> BudgetOut:
    _validate(body.period, body.limit_usd)
    budget = Budget(org_id=org.id, **body.model_dump())
    db.add(budget)
    await db.commit()
    statuses = await _evaluate(org.id, db)
    return _out(next(s for s in statuses if s.budget.id == budget.id))


@router.patch("/{budget_id}", response_model=BudgetOut)
async def update_budget(
    budget_id: str,
    body: UpdateBudgetRequest,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> BudgetOut:
    budget = await _get_budget(budget_id, org.id, db)
    _validate(body.period, body.limit_usd)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(budget, field, value)
    await db.commit()
    statuses = await _evaluate(org.id, db)
    return _out(next(s for s in statuses if s.budget.id == budget.id))


@router.delete("/{budget_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_budget(
    budget_id: str,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> Response:
    budget = await _get_budget(budget_id, org.id, db)
    await db.delete(budget)
    await db.flush()
    remaining = await evaluate_budgets(org.id, db, datetime.utcnow())
    await resolve_budget_alerts(org.id, budget_id, db, any_tripped=any(s.tripped for s in remaining))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

Evaluation after ingest — add to both ingest routers right after their `await db.commit()`:

```python
    await _evaluate_budgets_after_ingest(org.id, db)
```

with, in `server/app/budgets.py`:

```python
async def evaluate_after_ingest(org_id: str, db: AsyncSession) -> None:
    """Called after an ingest commit. Never raises."""
    try:
        await evaluate_budgets(org_id, db)
        await db.commit()
    except Exception as exc:
        logger.warning("Budget evaluation after ingest failed for org %s: %s", org_id, exc)
        await db.rollback()
```

and in `ingest.py` / `otlp.py` call `from app.budgets import evaluate_after_ingest` then `await evaluate_after_ingest(org.id, db)` after the commit.

`server/app/main.py` — import `budgets` router and `app.include_router(budgets.router)`; in `_alert_worker`, after `evaluate_all_rules(db)`:

```python
                        from app.budgets import evaluate_all_budgets
                        await evaluate_all_budgets(db)
```

- [ ] **Step 4: Run tests**

Run: `cd server && pytest -v tests/test_budgets.py && pytest && ruff check app tests`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/app server/tests/test_budgets.py
git commit -m "feat(server): budgets API, status endpoint, and evaluation after ingest"
```

---

### Task 3: SDK — `BudgetExceededError`, `BudgetGuard`, loop enforcement

**Files:**
- Modify: `agent_kit/exceptions.py`, `agent_kit/cloud/reporter.py`, `agent_kit/agent/agent.py`, `agent_kit/agent/loop.py`
- Create: `agent_kit/cloud/budgets.py`, `tests/test_budgets.py`

**Interfaces:**
- Produces: `BudgetExceededError(scope, limit_usd, spent_usd, budget_name=None, resets_at=None)`; `BudgetGuard(reporter, refresh_interval_s=30.0, fail_closed=False, http_client=None)` with `async check(agent_name, project)`, `record_spend(agent_name, project, usd)`; `CloudReporter.budget_guard(refresh_interval_s=30.0, fail_closed=False)`, `CloudReporter.api_key`, `CloudReporter.base_url`; `AgentConfig(max_run_cost_usd=None, enforce_budgets=False)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_budgets.py
"""Cost circuit breaker in the SDK: per-run caps and fleet budget enforcement."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from agent_kit import Agent, AgentConfig, tool
from agent_kit.cloud.budgets import BudgetGuard
from agent_kit.cloud.reporter import CloudReporter
from agent_kit.exceptions import BudgetExceededError
from agent_kit.providers.base import ProviderConfig
from agent_kit.types import CostSummary, Message, ToolCall, Turn


def budget(name: str = "support daily", limit: float = 10.0, spent: float = 0.0, tripped: bool = False) -> dict[str, Any]:
    return {"id": "b1", "name": name, "project": "*", "agent_name": "support", "period": "daily",
            "limit_usd": limit, "enabled": True, "spent_usd": spent, "remaining_usd": max(0.0, limit - spent),
            "tripped": tripped, "tripped_at": None, "period_start": "2026-09-14T00:00:00",
            "resets_at": "2026-09-15T00:00:00"}


class StatusServer:
    def __init__(self, budgets: list[dict[str, Any]] | None = None, fail: bool = False) -> None:
        self.budgets = budgets or []
        self.fail = fail
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            return httpx.Response(503)
        return httpx.Response(200, json={"budgets": self.budgets})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def reporter() -> CloudReporter:
    return CloudReporter(api_key="akt_test", project="prod", agent_name="support", base_url="https://cloud.test")


async def test_guard_allows_under_limit_and_sends_scope():
    server = StatusServer([budget(spent=2.0)])
    guard = BudgetGuard(reporter(), http_client=server.client())

    await guard.check("support", "prod")

    (request,) = server.requests
    assert request.url.path == "/v1/budgets/status"
    assert dict(request.url.params) == {"project": "prod", "agent_name": "support"}
    assert request.headers["authorization"] == "Bearer akt_test"


async def test_guard_trips_on_server_status():
    guard = BudgetGuard(reporter(), http_client=StatusServer([budget(limit=5.0, spent=6.0, tripped=True)]).client())

    with pytest.raises(BudgetExceededError) as info:
        await guard.check("support", "prod")

    err = info.value
    assert (err.scope, err.budget_name, err.limit_usd, err.spent_usd) == ("budget", "support daily", 5.0, 6.0)
    assert err.resets_at is not None and err.resets_at.isoformat() == "2026-09-15T00:00:00"


async def test_local_spend_since_refresh_counts_toward_limit():
    server = StatusServer([budget(limit=5.0, spent=4.0)])
    guard = BudgetGuard(reporter(), refresh_interval_s=3600, http_client=server.client())

    await guard.check("support", "prod")
    guard.record_spend("support", "prod", 0.6)
    await guard.check("support", "prod")
    guard.record_spend("support", "prod", 0.5)
    with pytest.raises(BudgetExceededError) as info:
        await guard.check("support", "prod")

    assert info.value.spent_usd == pytest.approx(5.1)
    assert len(server.requests) == 1  # cached within the refresh interval


async def test_guard_refreshes_after_interval_and_resets_local_spend():
    server = StatusServer([budget(limit=5.0, spent=4.0)])
    guard = BudgetGuard(reporter(), refresh_interval_s=0, http_client=server.client())

    await guard.check("support", "prod")
    guard.record_spend("support", "prod", 2.0)
    server.budgets = [budget(limit=5.0, spent=4.5)]
    await guard.check("support", "prod")  # fresh status; local spend already reported

    assert len(server.requests) == 2


async def test_guard_fails_open_by_default_and_closed_on_request():
    await BudgetGuard(reporter(), http_client=StatusServer(fail=True).client()).check("support", "prod")

    closed = BudgetGuard(reporter(), fail_closed=True, http_client=StatusServer(fail=True).client())
    with pytest.raises(BudgetExceededError) as info:
        await closed.check("support", "prod")
    assert (info.value.scope, info.value.budget_name) == ("budget", None)


def test_reporter_shares_one_guard():
    rep = reporter()
    assert rep.budget_guard() is rep.budget_guard()


class PricedProvider:
    """Each call costs $1 and requests a tool, forever."""

    config = ProviderConfig(default_model="mock")

    def __init__(self) -> None:
        self.calls = 0

    def name(self) -> str:
        return "mock"

    async def complete(self, messages: list[Message], **kw: Any) -> Turn:
        self.calls += 1
        call = ToolCall(tool_name="noop", arguments={}, call_id=f"c{self.calls}")
        return Turn(message_out=Message(role="assistant", content="", tool_calls=[call]), tool_calls=[call],
                    cost=CostSummary(total_tokens=10, cost_usd=1.0, model="mock"))

    async def stream(self, messages: list[Message], **kw: Any) -> Any:
        self.calls += 1
        call = ToolCall(tool_name="noop", arguments={}, call_id=f"s{self.calls}")
        yield Turn(message_out=Message(role="assistant", content="", tool_calls=[call]), tool_calls=[call],
                   cost=CostSummary(total_tokens=10, cost_usd=1.0, model="mock"))


@tool(description="does nothing")
def noop() -> str:
    return "ok"


@pytest.mark.parametrize("mode", ["run", "stream"])
async def test_per_run_cap_stops_before_the_next_call(mode):
    provider = PricedProvider()
    agent = Agent(provider, tools=[noop], config=AgentConfig(max_run_cost_usd=2.5, max_turns=10))

    with pytest.raises(BudgetExceededError) as info:
        if mode == "run":
            await agent.run("go")
        else:
            async for _ in agent.stream("go"):
                pass

    assert provider.calls == 3  # $1, $2, $3 — the call after reaching $2.50 never happens
    assert (info.value.scope, info.value.limit_usd, info.value.spent_usd) == ("run", 2.5, 3.0)
    assert agent.audit is not None
    assert agent.audit.events()[-1].event_type == "budget_exceeded"


async def test_enforce_budgets_requires_cloud():
    with pytest.raises(ValueError, match="cloud"):
        Agent(PricedProvider(), config=AgentConfig(enforce_budgets=True))


async def test_fleet_budget_stops_agent_and_records_spend(monkeypatch):
    server = StatusServer([budget(limit=2.0, spent=0.5)])
    rep = reporter()
    monkeypatch.setattr(rep, "submit_threadsafe", lambda event: None)
    monkeypatch.setattr(rep, "_enqueue", _noop_enqueue)
    rep._budget_guard = BudgetGuard(rep, refresh_interval_s=3600, http_client=server.client())
    provider = PricedProvider()
    agent = Agent(provider, tools=[noop], config=AgentConfig(cloud=rep, enforce_budgets=True, max_turns=10))

    with pytest.raises(BudgetExceededError) as info:
        await agent.run("go")

    assert provider.calls == 2  # $0.50 server + $1 + $1 local ≥ $2
    assert (info.value.scope, info.value.budget_name) == ("budget", "support daily")


async def _noop_enqueue(event: Any) -> None:
    return None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_budgets.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent_kit.cloud.budgets'`

- [ ] **Step 3: Implement**

`agent_kit/exceptions.py` — append:

```python
class BudgetExceededError(AgentKitError):
    """A per-run cost cap or a fleet budget stopped the agent before its next model call."""

    def __init__(
        self,
        scope: str,
        limit_usd: float,
        spent_usd: float,
        budget_name: str | None = None,
        resets_at: datetime | None = None,
    ) -> None:
        if scope == "run":
            message = f"Run cost ${spent_usd:.4f} reached the per-run cap of ${limit_usd:.4f}."
        elif budget_name is None:
            message = "Budget status unavailable and fail_closed is set; refusing to call the model."
        else:
            message = f"Budget '{budget_name}' exhausted: ${spent_usd:.4f} of ${limit_usd:.4f}."
            if resets_at is not None:
                message += f" Resets at {resets_at.isoformat()} UTC."
        super().__init__(message)
        self.scope = scope
        self.limit_usd = limit_usd
        self.spent_usd = spent_usd
        self.budget_name = budget_name
        self.resets_at = resets_at
```

(plus `from datetime import datetime` at the top of the module).

```python
# agent_kit/cloud/budgets.py
"""
BudgetGuard — enforce agent-kit Cloud fleet budgets in-process.

Fetches /v1/budgets/status for an agent at most every ``refresh_interval_s`` and adds
this process's spend since that fetch, so one process can't overshoot between
refreshes. If status can't be fetched the guard fails open unless ``fail_closed``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx

from agent_kit.exceptions import BudgetExceededError

if TYPE_CHECKING:
    from agent_kit.cloud.reporter import CloudReporter

logger = logging.getLogger("agent_kit.cloud")

_STATUS_PATH = "/v1/budgets/status"


@dataclass
class _Entry:
    fetched_at: float
    budgets: list[dict[str, Any]]
    local_spend_usd: float = 0.0


class BudgetGuard:
    def __init__(
        self,
        reporter: CloudReporter,
        refresh_interval_s: float = 30.0,
        fail_closed: bool = False,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._reporter = reporter
        self._refresh_interval_s = refresh_interval_s
        self._fail_closed = fail_closed
        self._http = http_client
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._lock = asyncio.Lock()

    async def check(self, agent_name: str, project: str) -> None:
        """Raise BudgetExceededError if any budget covering this agent is exhausted."""
        entry = await self._current(agent_name, project)
        if entry is None:
            if self._fail_closed:
                raise BudgetExceededError(scope="budget", limit_usd=0.0, spent_usd=0.0)
            return
        for budget in entry.budgets:
            limit = float(budget.get("limit_usd") or 0.0)
            spent = float(budget.get("spent_usd") or 0.0) + entry.local_spend_usd
            if budget.get("tripped") or spent >= limit:
                raise BudgetExceededError(
                    scope="budget",
                    limit_usd=limit,
                    spent_usd=spent,
                    budget_name=str(budget.get("name") or budget.get("id") or "budget"),
                    resets_at=_parse_time(budget.get("resets_at")),
                )

    def record_spend(self, agent_name: str, project: str, usd: float) -> None:
        """Count spend that the server's status doesn't include yet."""
        entry = self._entries.get((project, agent_name))
        if entry is not None and usd > 0:
            entry.local_spend_usd += usd

    async def _current(self, agent_name: str, project: str) -> _Entry | None:
        key = (project, agent_name)
        entry = self._entries.get(key)
        if entry is not None and time.monotonic() - entry.fetched_at < self._refresh_interval_s:
            return entry
        async with self._lock:
            entry = self._entries.get(key)
            if entry is not None and time.monotonic() - entry.fetched_at < self._refresh_interval_s:
                return entry
            budgets = await self._fetch(agent_name, project)
            if budgets is None:
                return entry  # keep the last known status, retry on the next check
            fresh = _Entry(fetched_at=time.monotonic(), budgets=budgets)
            self._entries[key] = fresh
            return fresh

    async def _fetch(self, agent_name: str, project: str) -> list[dict[str, Any]] | None:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(5.0))
        try:
            resp = await self._http.get(
                f"{self._reporter.base_url}{_STATUS_PATH}",
                params={"project": project, "agent_name": agent_name},
                headers={"Authorization": f"Bearer {self._reporter.api_key}"},
            )
            resp.raise_for_status()
            budgets = resp.json().get("budgets", [])
            return [b for b in budgets if isinstance(b, dict)]
        except Exception as exc:
            logger.debug("agent-kit Cloud: budget status unavailable: %s", exc)
            return None


def _parse_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None
```

`agent_kit/cloud/reporter.py` — in `__init__` add `self._budget_guard: BudgetGuard | None = None`; add:

```python
    @property
    def api_key(self) -> str:
        return self._api_key

    @property
    def base_url(self) -> str:
        return self._base_url

    def budget_guard(self, refresh_interval_s: float = 30.0, fail_closed: bool = False) -> BudgetGuard:
        """The reporter's shared BudgetGuard (created on first use with these settings)."""
        if self._budget_guard is None:
            from agent_kit.cloud.budgets import BudgetGuard

            self._budget_guard = BudgetGuard(self, refresh_interval_s=refresh_interval_s, fail_closed=fail_closed)
        return self._budget_guard
```

(with `from agent_kit.cloud.budgets import BudgetGuard` under `TYPE_CHECKING`).

`agent_kit/agent/agent.py` — `AgentConfig.__init__` gains `max_run_cost_usd: float | None = None, enforce_budgets: bool = False` stored on self; `Agent.__init__` raises

```python
        if self._config.enforce_budgets and self._config.cloud is None:
            raise ValueError("enforce_budgets=True requires AgentConfig(cloud=CloudReporter(...))")
```

`_make_loop` passes `max_run_cost_usd=self._config.max_run_cost_usd` and
`budget_guard=self._config.cloud.budget_guard() if self._config.enforce_budgets and self._config.cloud else None`.

`agent_kit/agent/loop.py` — `AgentLoop.__init__` gains `max_run_cost_usd: float | None = None, budget_guard: BudgetGuard | None = None` (stored; `self._run_cost_usd = 0.0`). In `_execute`, at the top of each `while` iteration before `messages = ...`:

```python
                    await self._enforce_budgets()
```

After the turn's cost is recorded (right after `self._tracer.record_cost(...)`):

```python
                    self._run_cost_usd += turn.cost.cost_usd
                    if self._budget_guard is not None and self._reporter is not None:
                        self._budget_guard.record_spend(
                            self._reporter.agent_name, self._reporter.project, turn.cost.cost_usd
                        )
```

New method:

```python
    async def _enforce_budgets(self) -> None:
        """Stop before a model call when the run cap or a fleet budget is exhausted."""
        try:
            if self._max_run_cost_usd is not None and self._run_cost_usd >= self._max_run_cost_usd:
                raise BudgetExceededError(
                    scope="run", limit_usd=self._max_run_cost_usd, spent_usd=self._run_cost_usd
                )
            if self._budget_guard is not None and self._reporter is not None:
                await self._budget_guard.check(self._reporter.agent_name, self._reporter.project)
        except BudgetExceededError as exc:
            if self._audit:
                self._audit.append(
                    "budget_exceeded",
                    actor=exc.budget_name or "run",
                    payload={"scope": exc.scope, "limit_usd": exc.limit_usd, "spent_usd": exc.spent_usd},
                )
            raise
```

Export `BudgetExceededError` from `agent_kit/__init__.py` if exceptions are exported there.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_budgets.py -v && pytest && ruff check agent_kit tests && mypy agent_kit`
Expected: PASS / clean

- [ ] **Step 5: Commit**

```bash
git add agent_kit tests/test_budgets.py
git commit -m "feat: per-run cost caps and fleet budget enforcement in the agent loop"
```

---

### Task 4: Claude Agent SDK enforcement

**Files:**
- Modify: `agent_kit/integrations/recorder.py`, `agent_kit/integrations/claude_agent_sdk.py`, `tests/test_integrations_claude.py`

**Interfaces:**
- Consumes: `BudgetGuard`, `BudgetExceededError` (Task 3).
- Produces: `RunRecorder.llm_turn(...) -> float` (priced cost; 0.0 if dropped); public `recorder.price_call(model, input_tokens, output_tokens, cache_read=0, cache_write=0) -> float`; `ClaudeAgentObserver(reporter, agent_name=None, max_run_cost_usd=None, enforce_budgets=False)`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_integrations_claude.py
from agent_kit.exceptions import BudgetExceededError


class TrippedGuard:
    def __init__(self, tripped: bool = True) -> None:
        self.tripped = tripped
        self.checks: list[tuple[str, str]] = []
        self.spend: list[float] = []

    async def check(self, agent_name: str, project: str) -> None:
        self.checks.append((agent_name, project))
        if self.tripped:
            raise BudgetExceededError(scope="budget", limit_usd=5.0, spent_usd=5.5, budget_name="claude daily")

    def record_spend(self, agent_name: str, project: str, usd: float) -> None:
        self.spend.append(usd)


async def test_tripped_budget_stops_claude_at_tool_boundary(cloud_capture):
    guard = TrippedGuard()
    cloud_capture.reporter._budget_guard = guard
    observer = ClaudeAgentObserver(cloud_capture.reporter, enforce_budgets=True)
    outputs: list[Any] = []

    async def source():
        yield INIT
        outputs.append(await observer._on_hook(hook("PreToolUse", tool_name="Bash", tool_use_id="t1", tool_input={}), "t1", None))
        outputs.append(await observer._on_hook(hook("SubagentStart", agent_id="a", agent_type="x"), None, None))
        yield ResultMessage()

    await collect(observer, source())

    pre, sub = outputs
    assert pre["continue_"] is False and "claude daily" in pre["stopReason"]
    assert pre["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert sub["continue_"] is False
    assert guard.checks == [("claude-agent", "proj"), ("claude-agent", "proj")]
    (run_id,) = {e.run_id for e in cloud_capture.events}
    assert cloud_capture.audit_types(run_id).count("budget_exceeded") == 2


async def test_budget_not_tripped_lets_hooks_pass_and_records_spend(cloud_capture):
    guard = TrippedGuard(tripped=False)
    cloud_capture.reporter._budget_guard = guard
    observer = ClaudeAgentObserver(cloud_capture.reporter, enforce_budgets=True)
    outputs: list[Any] = []

    async def source():
        yield INIT
        yield AssistantMessage([ToolUseBlock("t1", "Read", {})], "claude-opus-5", usage(1000, 100), "m1", "s1")
        outputs.append(await observer._on_hook(hook("PreToolUse", tool_name="Read", tool_use_id="t1", tool_input={}), "t1", None))
        yield ResultMessage()

    await collect(observer, source())

    assert outputs == [{}]
    assert guard.spend == [pytest.approx((1000 * 5 + 100 * 25) / 1_000_000)]


async def test_observer_without_enforcement_never_checks(cloud_capture):
    guard = TrippedGuard()
    cloud_capture.reporter._budget_guard = guard
    observer = ClaudeAgentObserver(cloud_capture.reporter)

    async def source():
        yield INIT
        assert await observer._on_hook(hook("PreToolUse", tool_name="Bash", tool_use_id="t", tool_input={}), "t", None) == {}
        yield ResultMessage()

    await collect(observer, source())
    assert guard.checks == []


@requires_claude_sdk
def test_with_hooks_sets_native_per_run_cap():
    from claude_agent_sdk import ClaudeAgentOptions

    from agent_kit.cloud.reporter import CloudReporter

    observer = ClaudeAgentObserver(CloudReporter(api_key="akt_test"), max_run_cost_usd=1.5)
    assert observer.with_hooks(ClaudeAgentOptions()).max_budget_usd == 1.5
    assert observer.with_hooks(ClaudeAgentOptions(max_budget_usd=0.25)).max_budget_usd == 0.25
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_integrations_claude.py -v -k "budget or enforcement or native"`
Expected: FAIL — `TypeError: ClaudeAgentObserver.__init__() got an unexpected keyword argument 'enforce_budgets'`

- [ ] **Step 3: Implement**

`agent_kit/integrations/recorder.py` — rename `_price` to `price_call` (keep behaviour) and make `llm_turn` return the priced cost:

```python
    ) -> float:
        cost = 0.0
        with self._guard("llm_turn"):
            ...  # unchanged body; assigns cost = price_call(...)
        return cost
```

`agent_kit/integrations/claude_agent_sdk.py`:

```python
from agent_kit.exceptions import BudgetExceededError
...
    def __init__(
        self,
        reporter: CloudReporter,
        agent_name: str | None = None,
        max_run_cost_usd: float | None = None,
        enforce_budgets: bool = False,
    ) -> None:
        self._reporter = reporter
        self._agent_name = reporter.agent_name or agent_name or "claude-agent"
        self._recorder = RunRecorder(reporter, harness=HARNESS, agent_name=agent_name or "claude-agent")
        self._max_run_cost_usd = max_run_cost_usd
        self._guard = reporter.budget_guard() if enforce_budgets else None
        ...

    def with_hooks(self, options: Any) -> Any:
        """Add agent-kit's hooks to ``options``, after any hooks already configured."""
        ...  # existing merge
        if self._max_run_cost_usd is not None and getattr(options, "max_budget_usd", None) is None:
            options.max_budget_usd = self._max_run_cost_usd
        return options

    async def _on_hook(
        self, input_data: Any, tool_use_id: str | None, context: Any
    ) -> HookJSONOutput:
        try:
            self._record_hook(input_data, tool_use_id)
        except Exception:
            logger.debug("ClaudeAgentObserver hook failed", exc_info=True)
        event = input_data.get("hook_event_name") if isinstance(input_data, dict) else None
        if self._guard is None or event not in ("PreToolUse", "SubagentStart"):
            return {}
        try:
            await self._guard.check(self._agent_name, self._reporter.project)
        except BudgetExceededError as exc:
            return self._stop(input_data, event, exc)
        except Exception:
            logger.debug("ClaudeAgentObserver budget check failed", exc_info=True)
        return {}

    def _stop(self, data: dict[str, Any], event: str, exc: BudgetExceededError) -> HookJSONOutput:
        reason = f"agent-kit: {exc}"
        with self._lock:
            observation = self._sessions.get(data.get("session_id") or "")
        if observation is not None:
            self._recorder.audit(
                observation.run_id,
                "budget_exceeded",
                actor=exc.budget_name or "run",
                payload={"scope": exc.scope, "limit_usd": exc.limit_usd, "spent_usd": exc.spent_usd},
            )
        output: dict[str, Any] = {"continue_": False, "stopReason": reason}
        if event == "PreToolUse":
            output["hookSpecificOutput"] = {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        return output  # type: ignore[return-value]

    def _record_spend(self, usd: float) -> None:
        if self._guard is not None:
            self._guard.record_spend(self._agent_name, self._reporter.project, usd)
```

In `_Observation.flush_turn`, capture the cost and record it:

```python
        cost = self._recorder.llm_turn(...)
        self._observer._record_spend(cost)
```

(If mypy reports the `type: ignore` as unused because the SDK isn't installed, keep the return typed via `cast("HookJSONOutput", output)` instead.)

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_integrations_claude.py tests/test_integrations_recorder.py -v && ruff check agent_kit tests && mypy agent_kit` (both with and without `claude-agent-sdk` installed)
Expected: PASS / clean

- [ ] **Step 5: Commit**

```bash
git add agent_kit/integrations tests/test_integrations_claude.py
git commit -m "feat: budget enforcement for Claude Agent SDK runs"
```

---

### Task 5: OpenAI Agents SDK enforcement — `AgentKitRunHooks`

**Files:**
- Modify: `agent_kit/integrations/openai_agents.py`, `tests/test_integrations_openai_agents.py`

**Interfaces:**
- Consumes: `BudgetGuard`, `BudgetExceededError`, `price_call`.
- Produces: `AgentKitRunHooks(reporter, agent_name=None, max_run_cost_usd=None, enforce_budgets=True)`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_integrations_openai_agents.py
from agent_kit.exceptions import BudgetExceededError
from agent_kit.integrations.openai_agents import AgentKitRunHooks


class GuardStub:
    def __init__(self, tripped: bool) -> None:
        self.tripped = tripped
        self.checks: list[tuple[str, str]] = []
        self.spend: list[float] = []

    async def check(self, agent_name: str, project: str) -> None:
        self.checks.append((agent_name, project))
        if self.tripped:
            raise BudgetExceededError(scope="budget", limit_usd=1.0, spent_usd=1.2, budget_name="openai daily")

    def record_spend(self, agent_name: str, project: str, usd: float) -> None:
        self.spend.append(usd)


def _never_called_model():
    from agents.models.interface import Model

    class NeverCalled(Model):
        async def get_response(self, *args, **kwargs):
            raise AssertionError("model must not be called when the budget is exhausted")

        def stream_response(self, *args, **kwargs):
            raise AssertionError("model must not be called when the budget is exhausted")

    return NeverCalled()


async def test_run_hooks_stop_runner_before_model_call(cloud_capture):
    from agents import Agent as OAIAgent
    from agents import RunConfig, Runner

    guard = GuardStub(tripped=True)
    cloud_capture.reporter._budget_guard = guard
    hooks = AgentKitRunHooks(cloud_capture.reporter, agent_name="support")

    with pytest.raises(BudgetExceededError) as info:
        await Runner.run(
            OAIAgent(name="support", instructions="x", model=_never_called_model()),
            "hi",
            hooks=hooks,
            run_config=RunConfig(tracing_disabled=True),
        )

    assert info.value.budget_name == "openai daily"
    assert guard.checks == [("support", "proj")]


async def test_run_hooks_per_run_cap_from_context_usage(cloud_capture):
    hooks = AgentKitRunHooks(cloud_capture.reporter, max_run_cost_usd=0.01, enforce_budgets=False)
    context = NS(usage=NS(input_tokens=2000, output_tokens=1000))  # gpt-4o: $0.015
    agent = NS(name="a", model="gpt-4o")

    with pytest.raises(BudgetExceededError) as info:
        await hooks.on_llm_start(context, agent, None, [])
    assert (info.value.scope, info.value.spent_usd) == ("run", pytest.approx(0.015))

    await AgentKitRunHooks(cloud_capture.reporter, max_run_cost_usd=1.0, enforce_budgets=False).on_llm_start(context, agent, None, [])


async def test_run_hooks_record_spend_on_llm_end(cloud_capture):
    guard = GuardStub(tripped=False)
    cloud_capture.reporter._budget_guard = guard
    hooks = AgentKitRunHooks(cloud_capture.reporter)
    agent = NS(name="support", model=NS(model="gpt-4o-mini"))

    await hooks.on_llm_start(NS(usage=NS(input_tokens=0, output_tokens=0)), agent, None, [])
    await hooks.on_llm_end(NS(), agent, NS(usage=NS(input_tokens=1_000_000, output_tokens=0)))

    assert guard.spend == [pytest.approx(0.15)]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_integrations_openai_agents.py -v -k run_hooks`
Expected: FAIL — `ImportError: cannot import name 'AgentKitRunHooks'`

- [ ] **Step 3: Implement** — in `agent_kit/integrations/openai_agents.py`:

```python
try:
    from agents import RunHooks
    from agents.tracing import TracingProcessor
except ImportError as e:
    ...

from agent_kit.exceptions import BudgetExceededError
from agent_kit.integrations.recorder import RunRecorder, price_call

if TYPE_CHECKING:
    from agents import Agent, ModelResponse, RunContextWrapper, TResponseInputItem
    ...


def _model_name(agent: Any) -> str:
    model = getattr(agent, "model", None)
    if isinstance(model, str):
        return model
    name = getattr(model, "model", None)
    return name if isinstance(name, str) else ""


class AgentKitRunHooks(RunHooks[Any]):
    """
    Enforce agent-kit cost ceilings on OpenAI Agents SDK runs.

        Runner.run(agent, input, hooks=AgentKitRunHooks(reporter, max_run_cost_usd=2.0))

    Raises BudgetExceededError from on_llm_start — before the model is called — when the
    run's spend reaches ``max_run_cost_usd`` or a fleet budget covering the agent is
    exhausted. Set CloudReporter(agent_name=...) so budgets and reported runs share a name.
    """

    def __init__(
        self,
        reporter: CloudReporter,
        agent_name: str | None = None,
        max_run_cost_usd: float | None = None,
        enforce_budgets: bool = True,
    ) -> None:
        self._reporter = reporter
        self._agent_name = agent_name
        self._max_run_cost_usd = max_run_cost_usd
        self._guard = reporter.budget_guard() if enforce_budgets else None

    def _name(self, agent: Any) -> str:
        return self._reporter.agent_name or self._agent_name or str(getattr(agent, "name", "") or "agent")

    async def on_llm_start(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        system_prompt: str | None,
        input_items: list[TResponseInputItem],
    ) -> None:
        if self._max_run_cost_usd is not None:
            usage = context.usage
            spent = price_call(_model_name(agent), int(usage.input_tokens or 0), int(usage.output_tokens or 0))
            if spent >= self._max_run_cost_usd:
                raise BudgetExceededError(scope="run", limit_usd=self._max_run_cost_usd, spent_usd=spent)
        if self._guard is not None:
            await self._guard.check(self._name(agent), self._reporter.project)

    async def on_llm_end(
        self, context: RunContextWrapper[Any], agent: Agent[Any], response: ModelResponse
    ) -> None:
        if self._guard is None:
            return
        try:
            usage = response.usage
            cost = price_call(_model_name(agent), int(usage.input_tokens or 0), int(usage.output_tokens or 0))
            self._guard.record_spend(self._name(agent), self._reporter.project, cost)
        except Exception:
            logger.debug("AgentKitRunHooks.on_llm_end failed", exc_info=True)
```

- [ ] **Step 4: Run tests** (with and without `openai-agents` installed)

Run: `pytest tests/test_integrations_openai_agents.py -v && pytest && ruff check agent_kit tests && mypy agent_kit`
Expected: PASS / clean

- [ ] **Step 5: Commit**

```bash
git add agent_kit/integrations/openai_agents.py tests/test_integrations_openai_agents.py
git commit -m "feat: AgentKitRunHooks — budget enforcement for OpenAI Agents SDK runs"
```

---

### Task 6: Docs and end-to-end check

**Files:**
- Modify: `docs/api-reference.md`, `docs/cloud-quickstart.md`, `README.md`, `CHANGELOG.md`, `specs/06-harness-roadmap.md`, `specs/09-cost-circuit-breaker.md`, `PROJECT_INDEX.md`

- [ ] **Step 1: API reference** — new `## Budgets` section documenting the five routes with request/response examples (fields from `BudgetOut`), period semantics, spend definition (snapshots + in-flight runs), and the `budget_exceeded` alert rule (config, context fields, fire/resolve transitions). Add `budget_exceeded` to the alert rule types table.

- [ ] **Step 2: Quickstart + README** — a "Stop runaway spend" section:

```python
agent = Agent(
    AnthropicProvider(),
    config=AgentConfig(
        cloud=CloudReporter(project="support", agent_name="support-bot"),
        max_run_cost_usd=2.00,   # per run
        enforce_budgets=True,    # fleet budgets from agent-kit Cloud
    ),
)
```

```bash
curl -X POST https://ingest.agentkit.io/v1/budgets -H "Authorization: Bearer $AGENTKIT_API_KEY" \
  -d '{"name": "support daily", "period": "daily", "limit_usd": 200, "agent_name": "support-bot"}'
```

plus the adapter snippets (`ClaudeAgentObserver(..., max_run_cost_usd=2.0, enforce_budgets=True)`, `Runner.run(..., hooks=AgentKitRunHooks(reporter, max_run_cost_usd=2.0))`) and the overshoot bounds from the spec. Add a README "Built in" row: `Cost circuit breaker | Per-run caps and daily/weekly/monthly fleet budgets stop agents before the next model call and alert when tripped`.

- [ ] **Step 3: CHANGELOG `[Unreleased]` → `### Added`**

```markdown
- **Cost circuit breaker.** `AgentConfig(max_run_cost_usd=...)` stops a run before the model call after its spend reaches the cap; `AgentConfig(enforce_budgets=True)` enforces fleet budgets — daily/weekly/monthly UTC ceilings per agent, project, or org defined at `/v1/budgets` — raising `BudgetExceededError` before the next model call. Spend includes in-flight runs; `budget_exceeded` alert rules fire on trip and resolve on reset or a raised limit. Claude Agent SDK (`ClaudeAgentObserver(..., enforce_budgets=True)`) and OpenAI Agents SDK (`AgentKitRunHooks`) agents can be stopped too. Migration `006` adds `budgets`.
```

- [ ] **Step 4: Specs and index** — spec 09 status `implemented`; roadmap `3.2` ticked; `PROJECT_INDEX.md` gains `server/app/budgets.py`, `routers/budgets.py`, migration `006`, `agent_kit/cloud/budgets.py`, new test files, spec 09, and the budgets API section.

- [ ] **Step 5: End-to-end, then commit**

Run a local server on a fresh migrated database with a seeded key and a local webhook receiver (`http.server` in a thread recording POST bodies). Create a webhook channel and a `budget_exceeded` rule (`budget_id: "*"`), and a daily budget of `$0.02` for agent `e2e-agent`. Run an agent-kit `Agent` with `CloudReporter(base_url=local, agent_name="e2e-agent")`, a provider whose turns cost `$0.015`, and `enforce_budgets=True` + `refresh_interval_s=0` via `reporter.budget_guard(refresh_interval_s=0)`; flush the reporter after each run. Expect: run 1 and run 2 succeed; the budget reports `tripped`; the webhook receiver got an `alert.firing` payload for the budget; run 3 raises `BudgetExceededError` with zero provider calls; `PATCH` the limit to `$1` → budget `tripped: false` and the receiver gets `alert.resolved`.

```bash
git add docs README.md CHANGELOG.md specs PROJECT_INDEX.md
git commit -m "docs: cost circuit breaker — budgets API, quickstart, and changelog"
```
