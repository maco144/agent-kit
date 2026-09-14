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
