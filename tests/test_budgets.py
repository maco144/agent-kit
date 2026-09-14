"""Cost circuit breaker in the SDK: per-run caps and fleet budget enforcement."""

from __future__ import annotations

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
