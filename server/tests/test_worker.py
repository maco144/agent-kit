"""The background evaluation loop: one implementation, used by the worker container and main.py."""

from __future__ import annotations

import asyncio
import logging

import pytest

from app import worker


class _Session:
    """Hands the worker the test session without closing it."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def _session_factory(monkeypatch, db):
    """Point the worker at the test database (conftest's session)."""
    monkeypatch.setattr(worker, "SessionLocal", lambda: _Session(db))


async def test_run_cycle_evaluates_rules_budgets_and_retention(monkeypatch):
    called: list[str] = []

    def recorder(name):
        async def inner(db):
            called.append(name)
        return inner

    monkeypatch.setattr("app.alerting.evaluator.evaluate_all_rules", recorder("rules"))
    monkeypatch.setattr("app.budgets.evaluate_all_budgets", recorder("budgets"))
    monkeypatch.setattr("app.compliance.retention.purge_expired", recorder("retention"))

    await worker.run_cycle()

    assert called == ["rules", "budgets", "retention"]


async def test_run_forever_logs_errors_and_keeps_going(monkeypatch, caplog):
    attempts: list[int] = []

    async def boom(db):
        attempts.append(1)
        raise RuntimeError("db down")

    monkeypatch.setattr("app.alerting.evaluator.evaluate_all_rules", boom)

    with caplog.at_level(logging.WARNING, logger="agentkit.cloud.worker"):
        task = asyncio.create_task(worker.run_forever(cycle_seconds=0.01))
        await asyncio.sleep(0.08)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert len(attempts) >= 2  # the loop survived the first failure
    assert any("db down" in r.getMessage() for r in caplog.records)


async def test_a_rule_that_fails_to_flush_does_not_stop_the_cycle(monkeypatch, db):
    import uuid

    from app.models import AlertRule, Budget, CloudEventLog

    org_a, org_b = str(uuid.uuid4()), str(uuid.uuid4())
    db.add_all([
        AlertRule(org_id=org_a, name="broken", type="cost_anomaly"),
        AlertRule(org_id=org_b, name="healthy", type="cost_anomaly"),
        Budget(org_id=org_b, name="daily", period="daily", limit_usd=10.0),
    ])
    await db.commit()

    evaluated: list[str] = []

    async def eval_rule(rule, session):
        if rule.name == "broken":
            session.add(CloudEventLog(event_id=str(uuid.uuid4()), org_id=rule.org_id, run_id="r",
                                      agent_name=None, project="p", event_type="x", payload={}))
            await session.flush()  # NOT NULL agent_name: IntegrityError
        evaluated.append(rule.name)

    async def eval_budgets(org_id, session, now=None):
        evaluated.append(f"budgets:{org_id}")

    monkeypatch.setattr("app.alerting.evaluator._eval_cost_anomaly", eval_rule)
    monkeypatch.setattr("app.budgets.evaluate_budgets", eval_budgets)

    await worker.run_cycle()

    assert "healthy" in evaluated and f"budgets:{org_b}" in evaluated
