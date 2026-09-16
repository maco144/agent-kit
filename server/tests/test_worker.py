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
