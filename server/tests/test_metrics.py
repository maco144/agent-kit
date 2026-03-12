"""Integration tests for the fleet dashboard metrics API."""

from __future__ import annotations

import gzip
import json
import uuid
from datetime import datetime

import pytest
from sqlalchemy import select

from app.models import ActiveRunCache, AgentMetricSnapshot, CircuitBreakerEvent


# ---------------------------------------------------------------------------
# Helpers — reuse the NDJSON encoding from test_ingest.py pattern
# ---------------------------------------------------------------------------


def ndjson_body(events: list[dict]) -> bytes:
    ndjson = "\n".join(json.dumps(e) for e in events).encode()
    return gzip.compress(ndjson)


def _headers() -> dict[str, str]:
    return {"Content-Encoding": "gzip", "Content-Type": "application/x-ndjson"}


def make_run_start(run_id: str, model: str = "claude-sonnet-4-6", agent: str = "test-agent") -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": "run_start",
        "run_id": run_id,
        "agent_name": agent,
        "project": "test",
        "occurred_at": datetime.utcnow().isoformat(),
        "payload": {"model": model, "prompt_hash": "abc123"},
    }


def make_turn_complete(run_id: str, agent: str = "test-agent", input_tokens: int = 100,
                       output_tokens: int = 50, cost_usd: float = 0.005) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": "turn_complete",
        "run_id": run_id,
        "agent_name": agent,
        "project": "test",
        "occurred_at": datetime.utcnow().isoformat(),
        "payload": {
            "turn_index": 0,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
            "duration_ms": 1200,
            "tool_names": [],
        },
    }


def make_run_complete(run_id: str, agent: str = "test-agent", total_turns: int = 1,
                      total_cost_usd: float = 0.005) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": "run_complete",
        "run_id": run_id,
        "agent_name": agent,
        "project": "test",
        "occurred_at": datetime.utcnow().isoformat(),
        "payload": {
            "total_cost_usd": total_cost_usd,
            "total_tokens": 150,
            "total_turns": total_turns,
            "audit_root_hash": "0" * 64,
        },
    }


def make_run_error(run_id: str, agent: str = "test-agent", turn_count: int = 1) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": "run_error",
        "run_id": run_id,
        "agent_name": agent,
        "project": "test",
        "occurred_at": datetime.utcnow().isoformat(),
        "payload": {
            "error_type": "ProviderError",
            "error_message": "rate limit exceeded",
            "turn_count": turn_count,
        },
    }


def make_circuit_state_change(run_id: str, prev: str, new: str, agent: str = "test-agent") -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": "circuit_state_change",
        "run_id": run_id,
        "agent_name": agent,
        "project": "test",
        "occurred_at": datetime.utcnow().isoformat(),
        "payload": {
            "resource": "anthropic",
            "prev_state": prev,
            "new_state": new,
            "failure_count": 5,
        },
    }


# ---------------------------------------------------------------------------
# Pipeline unit tests — verify DB state after ingest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_start_creates_active_run_cache(client, db):
    run_id = str(uuid.uuid4())
    await client.post("/v1/events", content=ndjson_body([make_run_start(run_id)]), headers=_headers())

    result = await db.execute(select(ActiveRunCache).where(ActiveRunCache.run_id == run_id))
    cache = result.scalar_one_or_none()
    assert cache is not None
    assert cache.model == "claude-sonnet-4-6"
    assert cache.turns_so_far == 0
    assert cache.cost_so_far_usd == 0.0


@pytest.mark.asyncio
async def test_turn_complete_updates_active_run_cache(client, db):
    run_id = str(uuid.uuid4())
    await client.post(
        "/v1/events",
        content=ndjson_body([
            make_run_start(run_id),
            make_turn_complete(run_id, input_tokens=200, output_tokens=80, cost_usd=0.01),
        ]),
        headers=_headers(),
    )

    result = await db.execute(select(ActiveRunCache).where(ActiveRunCache.run_id == run_id))
    cache = result.scalar_one_or_none()
    assert cache is not None
    assert cache.turns_so_far == 1
    assert cache.input_tokens == 200
    assert cache.output_tokens == 80
    assert abs(cache.cost_so_far_usd - 0.01) < 1e-9


@pytest.mark.asyncio
async def test_run_complete_creates_snapshot_and_removes_cache(client, db):
    run_id = str(uuid.uuid4())
    await client.post(
        "/v1/events",
        content=ndjson_body([
            make_run_start(run_id),
            make_turn_complete(run_id, input_tokens=100, output_tokens=50, cost_usd=0.005),
            make_run_complete(run_id, total_turns=1, total_cost_usd=0.005),
        ]),
        headers=_headers(),
    )

    # ActiveRunCache should be gone
    cache_result = await db.execute(select(ActiveRunCache).where(ActiveRunCache.run_id == run_id))
    assert cache_result.scalar_one_or_none() is None

    # AgentMetricSnapshot should exist
    snap_result = await db.execute(
        select(AgentMetricSnapshot).where(AgentMetricSnapshot.agent_name == "test-agent")
    )
    snap = snap_result.scalar_one_or_none()
    assert snap is not None
    assert snap.runs_total == 1
    assert snap.runs_success == 1
    assert snap.runs_error == 0
    assert snap.input_tokens == 100
    assert snap.output_tokens == 50
    assert snap.total_turns == 1


@pytest.mark.asyncio
async def test_run_error_creates_snapshot_with_error_count(client, db):
    run_id = str(uuid.uuid4())
    agent = f"error-agent-{uuid.uuid4().hex[:6]}"
    await client.post(
        "/v1/events",
        content=ndjson_body([
            make_run_start(run_id, agent=agent),
            make_run_error(run_id, agent=agent, turn_count=2),
        ]),
        headers=_headers(),
    )

    snap_result = await db.execute(
        select(AgentMetricSnapshot).where(AgentMetricSnapshot.agent_name == agent)
    )
    snap = snap_result.scalar_one_or_none()
    assert snap is not None
    assert snap.runs_error == 1
    assert snap.runs_success == 0
    assert snap.total_turns == 2


@pytest.mark.asyncio
async def test_circuit_state_change_creates_event(client, db):
    run_id = str(uuid.uuid4())
    agent = f"cb-agent-{uuid.uuid4().hex[:6]}"
    await client.post(
        "/v1/events",
        content=ndjson_body([make_circuit_state_change(run_id, "closed", "open", agent=agent)]),
        headers=_headers(),
    )

    result = await db.execute(
        select(CircuitBreakerEvent).where(CircuitBreakerEvent.agent_name == agent)
    )
    ev = result.scalar_one_or_none()
    assert ev is not None
    assert ev.prev_state == "closed"
    assert ev.new_state == "open"
    assert ev.failure_count == 5
    assert ev.resource == "anthropic"


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_empty(client):
    resp = await client.get("/v1/metrics/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_runs"] == 0
    assert data["active_runs"] == 0
    assert data["agents_count"] == 0


@pytest.mark.asyncio
async def test_summary_with_data(client, db):
    agent = f"summary-agent-{uuid.uuid4().hex[:6]}"
    run_id = str(uuid.uuid4())
    await client.post(
        "/v1/events",
        content=ndjson_body([
            make_run_start(run_id, agent=agent),
            make_turn_complete(run_id, agent=agent, input_tokens=500, output_tokens=200, cost_usd=0.02),
            make_run_complete(run_id, agent=agent, total_turns=1),
        ]),
        headers=_headers(),
    )

    resp = await client.get("/v1/metrics/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_runs"] >= 1
    assert data["total_input_tokens"] >= 500
    assert data["total_cost_usd"] >= 0.0


@pytest.mark.asyncio
async def test_active_runs_shows_in_progress(client, db):
    agent = f"active-agent-{uuid.uuid4().hex[:6]}"
    run_id = str(uuid.uuid4())

    # Only run_start — no run_complete, so it stays in active cache
    await client.post(
        "/v1/events",
        content=ndjson_body([make_run_start(run_id, agent=agent)]),
        headers=_headers(),
    )

    resp = await client.get("/v1/metrics/active")
    assert resp.status_code == 200
    data = resp.json()
    run_ids = [r["run_id"] for r in data["active_runs"]]
    assert run_id in run_ids
    assert data["count"] >= 1

    # Find our specific run
    our_run = next(r for r in data["active_runs"] if r["run_id"] == run_id)
    assert our_run["agent_name"] == agent
    assert our_run["model"] == "claude-sonnet-4-6"
    assert our_run["elapsed_ms"] >= 0


@pytest.mark.asyncio
async def test_active_runs_removed_after_complete(client, db):
    agent = f"complete-agent-{uuid.uuid4().hex[:6]}"
    run_id = str(uuid.uuid4())

    await client.post(
        "/v1/events",
        content=ndjson_body([
            make_run_start(run_id, agent=agent),
            make_run_complete(run_id, agent=agent),
        ]),
        headers=_headers(),
    )

    resp = await client.get("/v1/metrics/active")
    data = resp.json()
    run_ids = [r["run_id"] for r in data["active_runs"]]
    assert run_id not in run_ids


@pytest.mark.asyncio
async def test_cost_endpoint_returns_series(client, db):
    agent = f"cost-agent-{uuid.uuid4().hex[:6]}"
    run_id = str(uuid.uuid4())
    await client.post(
        "/v1/events",
        content=ndjson_body([
            make_run_start(run_id, agent=agent),
            make_turn_complete(run_id, agent=agent, cost_usd=0.03),
            make_run_complete(run_id, agent=agent),
        ]),
        headers=_headers(),
    )

    resp = await client.get("/v1/metrics/cost")
    assert resp.status_code == 200
    data = resp.json()
    assert "series" in data
    assert "resolution" in data
    assert "group_by" in data
    # Our agent's series should appear
    labels = [s["label"] for s in data["series"]]
    assert agent in labels


@pytest.mark.asyncio
async def test_runs_endpoint_returns_series(client, db):
    agent = f"runs-agent-{uuid.uuid4().hex[:6]}"
    run_id = str(uuid.uuid4())
    await client.post(
        "/v1/events",
        content=ndjson_body([
            make_run_start(run_id, agent=agent),
            make_run_complete(run_id, agent=agent, total_turns=3),
        ]),
        headers=_headers(),
    )

    resp = await client.get("/v1/metrics/runs")
    assert resp.status_code == 200
    data = resp.json()
    assert "series" in data
    labels = [s["label"] for s in data["series"]]
    assert agent in labels
    series = next(s for s in data["series"] if s["label"] == agent)
    assert len(series["data"]) >= 1
    assert series["data"][0]["runs_total"] == 1
    assert series["data"][0]["avg_turns"] == 3.0


@pytest.mark.asyncio
async def test_agents_endpoint_with_cb_state(client, db):
    agent = f"fleet-agent-{uuid.uuid4().hex[:6]}"
    run_id = str(uuid.uuid4())
    await client.post(
        "/v1/events",
        content=ndjson_body([
            make_run_start(run_id, agent=agent),
            make_run_complete(run_id, agent=agent),
            make_circuit_state_change(run_id, "closed", "open", agent=agent),
        ]),
        headers=_headers(),
    )

    resp = await client.get("/v1/metrics/agents")
    assert resp.status_code == 200
    data = resp.json()
    agents_list = data["agents"]
    our = next((a for a in agents_list if a["agent_name"] == agent), None)
    assert our is not None
    assert our["runs_total"] == 1
    assert our["circuit_breaker_state"] == "open"


@pytest.mark.asyncio
async def test_circuit_breaker_endpoint(client, db):
    agent = f"cb-hist-agent-{uuid.uuid4().hex[:6]}"
    run_id = str(uuid.uuid4())
    await client.post(
        "/v1/events",
        content=ndjson_body([
            make_circuit_state_change(run_id, "closed", "open", agent=agent),
            make_circuit_state_change(run_id, "open", "half_open", agent=agent),
            make_circuit_state_change(run_id, "half_open", "closed", agent=agent),
        ]),
        headers=_headers(),
    )

    resp = await client.get("/v1/metrics/circuit-breaker")
    assert resp.status_code == 200
    data = resp.json()
    our = next((a for a in data["agents"] if a["agent_name"] == agent), None)
    assert our is not None
    assert our["current_state"] == "closed"
    assert len(our["events"]) == 3
    # The open→half_open transition should have duration_open_ms set
    open_event = next(e for e in our["events"] if e["new_state"] == "open")
    assert open_event["duration_open_ms"] is not None
    assert open_event["duration_open_ms"] >= 0


@pytest.mark.asyncio
async def test_metrics_require_auth():
    from httpx import ASGITransport, AsyncClient
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        resp = await anon.get("/v1/metrics/summary")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_snapshot_accumulates_multiple_runs(client, db):
    """Two runs completing in the same minute bucket should sum their metrics."""
    agent = f"multi-run-{uuid.uuid4().hex[:6]}"
    for _ in range(3):
        run_id = str(uuid.uuid4())
        await client.post(
            "/v1/events",
            content=ndjson_body([
                make_run_start(run_id, agent=agent),
                make_turn_complete(run_id, agent=agent, input_tokens=100, output_tokens=50, cost_usd=0.01),
                make_run_complete(run_id, agent=agent, total_turns=1),
            ]),
            headers=_headers(),
        )

    snap_result = await db.execute(
        select(AgentMetricSnapshot).where(AgentMetricSnapshot.agent_name == agent)
    )
    snaps = list(snap_result.scalars().all())
    total_runs = sum(s.runs_total for s in snaps)
    assert total_runs == 3
    total_tokens = sum(s.input_tokens for s in snaps)
    assert total_tokens == 300
