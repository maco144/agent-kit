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
            async with db.begin_nested():  # one org's failure rolls back only that org
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


async def evaluate_after_ingest(org_id: str, db: AsyncSession) -> None:
    """Called after an ingest commit. Never raises."""
    try:
        await evaluate_budgets(org_id, db)
        await db.commit()
    except Exception as exc:
        logger.warning("Budget evaluation after ingest failed for org %s: %s", org_id, exc)
        await db.rollback()
