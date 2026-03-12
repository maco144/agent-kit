"""Alert rule evaluator — condition checks and firing lifecycle management."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentMetricSnapshot, AlertFiring, AlertRule

logger = logging.getLogger("agentkit.cloud.alerts")


# ---------------------------------------------------------------------------
# Background worker entry point
# ---------------------------------------------------------------------------


async def evaluate_all_rules(db: AsyncSession) -> None:
    """
    Evaluate all polled alert rules (cost_anomaly, error_rate).
    Called by the background worker every 60 seconds.
    circuit_breaker_open and audit_integrity_failure are event-driven.
    """
    now = datetime.utcnow()
    result = await db.execute(
        select(AlertRule).where(AlertRule.enabled == True)  # noqa: E712
    )
    rules = list(result.scalars().all())

    for rule in rules:
        if rule.muted_until and rule.muted_until > now:
            continue
        try:
            if rule.type == "cost_anomaly":
                await _eval_cost_anomaly(rule, db)
            elif rule.type == "error_rate":
                await _eval_error_rate(rule, db)
            # circuit_breaker_open + audit_integrity_failure are event-driven
        except Exception as exc:
            logger.warning("Rule %s evaluation failed: %s", rule.id, exc)


# ---------------------------------------------------------------------------
# Event-driven triggers (called from ingest handlers)
# ---------------------------------------------------------------------------


async def fire_circuit_breaker_open(
    org_id: str,
    agent_name: str,
    project: str,
    resource: str,
    failure_count: int,
    occurred_at: datetime,
    db: AsyncSession,
) -> None:
    """Trigger circuit_breaker_open alert when CB transitions to open."""
    rules = await _matching_cb_rules(org_id, agent_name, project, resource, db)
    for rule in rules:
        ctx = {
            "agent_name": agent_name,
            "project": project,
            "resource": resource,
            "failure_count": failure_count,
            "occurred_at": occurred_at.isoformat(),
        }
        await _create_firing(rule, ctx, db)


async def resolve_circuit_breaker(
    org_id: str,
    agent_name: str,
    resource: str,
    db: AsyncSession,
) -> None:
    """Resolve circuit_breaker_open alerts when CB transitions to closed."""
    rules = await _matching_cb_rules(org_id, agent_name, project=None, resource=resource, db=db)
    for rule in rules:
        firing = await _get_active_firing(rule.id, db)
        if firing:
            await _resolve_firing(rule, firing, db)


async def fire_audit_integrity_failure(
    org_id: str,
    agent_name: str,
    project: str,
    run_id: str,
    db: AsyncSession,
) -> None:
    """Trigger audit_integrity_failure alert when chain verification fails."""
    now = datetime.utcnow()
    result = await db.execute(
        select(AlertRule).where(
            AlertRule.org_id == org_id,
            AlertRule.type == "audit_integrity_failure",
            AlertRule.enabled == True,  # noqa: E712
        )
    )
    rules = list(result.scalars().all())
    for rule in rules:
        if rule.muted_until and rule.muted_until > now:
            continue
        cfg = rule.config
        if not _matches_wildcard(cfg.get("agent_name", "*"), agent_name):
            continue
        if not _matches_wildcard(cfg.get("project", "*"), project):
            continue
        ctx = {"run_id": run_id, "agent_name": agent_name, "project": project}
        await _create_firing(rule, ctx, db)


# ---------------------------------------------------------------------------
# Polled evaluators
# ---------------------------------------------------------------------------


async def _eval_cost_anomaly(rule: AlertRule, db: AsyncSession) -> None:
    cfg = rule.config
    agent = cfg.get("agent_name", "*")
    window = int(cfg.get("window_minutes", 60))
    mode = cfg.get("mode", "absolute")

    since = datetime.utcnow() - timedelta(minutes=window)
    q = select(AgentMetricSnapshot).where(
        AgentMetricSnapshot.org_id == rule.org_id,
        AgentMetricSnapshot.bucket >= since,
    )
    if agent != "*":
        q = q.where(AgentMetricSnapshot.agent_name == agent)

    result = await db.execute(q)
    snaps = list(result.scalars().all())
    cost = sum(s.cost_usd for s in snaps)

    condition_met = False
    context: dict = {}

    if mode == "absolute":
        threshold = float(cfg.get("threshold_usd", 0))
        condition_met = cost > threshold
        context = {
            "cost_usd_in_window": round(cost, 6),
            "threshold_usd": threshold,
            "window_minutes": window,
            "agent_name": agent,
        }
    # relative mode (baseline comparison) deferred to a later iteration

    await _update_firing_state(rule, condition_met, context, db)


async def _eval_error_rate(rule: AlertRule, db: AsyncSession) -> None:
    cfg = rule.config
    agent = cfg.get("agent_name", "*")
    window = int(cfg.get("window_minutes", 15))
    threshold_pct = float(cfg.get("threshold_pct", 10.0))
    min_runs = int(cfg.get("min_runs", 5))

    since = datetime.utcnow() - timedelta(minutes=window)
    q = select(AgentMetricSnapshot).where(
        AgentMetricSnapshot.org_id == rule.org_id,
        AgentMetricSnapshot.bucket >= since,
    )
    if agent != "*":
        q = q.where(AgentMetricSnapshot.agent_name == agent)

    result = await db.execute(q)
    snaps = list(result.scalars().all())
    total = sum(s.runs_total for s in snaps)
    errors = sum(s.runs_error for s in snaps)

    if total < min_runs:
        condition_met = False
    else:
        error_pct = errors / total * 100
        condition_met = error_pct > threshold_pct

    context = {
        "total_runs": total,
        "error_runs": errors,
        "error_rate_pct": round(errors / total * 100, 2) if total > 0 else 0.0,
        "threshold_pct": threshold_pct,
        "window_minutes": window,
        "agent_name": agent,
    }
    await _update_firing_state(rule, condition_met, context, db)


# ---------------------------------------------------------------------------
# Firing lifecycle helpers
# ---------------------------------------------------------------------------


async def _update_firing_state(
    rule: AlertRule, condition_met: bool, context: dict, db: AsyncSession
) -> None:
    existing = await _get_active_firing(rule.id, db)
    if condition_met and existing is None:
        await _create_firing(rule, context, db)
    elif not condition_met and existing is not None:
        await _resolve_firing(rule, existing, db)


async def _create_firing(
    rule: AlertRule, context: dict, db: AsyncSession
) -> AlertFiring | None:
    """Create a new AlertFiring if none already active (deduplication)."""
    existing = await _get_active_firing(rule.id, db)
    if existing is not None:
        return None  # deduplicated

    from app.alerting.dispatch import dispatch_alert
    firing = AlertFiring(
        rule_id=rule.id,
        org_id=rule.org_id,
        state="firing",
        fired_at=datetime.utcnow(),
        context=context,
    )
    db.add(firing)
    await db.flush()  # assign ID before dispatch
    await dispatch_alert(rule, firing, event="alert.firing", db=db)
    return firing


async def _resolve_firing(
    rule: AlertRule, firing: AlertFiring, db: AsyncSession
) -> None:
    """Mark a firing as resolved. audit_integrity_failure never auto-resolves."""
    if rule.type == "audit_integrity_failure":
        return

    from app.alerting.dispatch import dispatch_alert
    firing.state = "resolved"
    firing.resolved_at = datetime.utcnow()
    await dispatch_alert(rule, firing, event="alert.resolved", db=db)


async def _get_active_firing(rule_id: str, db: AsyncSession) -> AlertFiring | None:
    result = await db.execute(
        select(AlertFiring).where(
            AlertFiring.rule_id == rule_id,
            AlertFiring.state == "firing",
        )
    )
    return result.scalar_one_or_none()


async def _matching_cb_rules(
    org_id: str,
    agent_name: str,
    project: str | None,
    resource: str,
    db: AsyncSession,
) -> list[AlertRule]:
    now = datetime.utcnow()
    result = await db.execute(
        select(AlertRule).where(
            AlertRule.org_id == org_id,
            AlertRule.type == "circuit_breaker_open",
            AlertRule.enabled == True,  # noqa: E712
        )
    )
    rules = list(result.scalars().all())
    matched = []
    for rule in rules:
        if rule.muted_until and rule.muted_until > now:
            continue
        cfg = rule.config
        if not _matches_wildcard(cfg.get("agent_name", "*"), agent_name):
            continue
        if project and cfg.get("project") and not _matches_wildcard(cfg["project"], project):
            continue
        if cfg.get("resource") and not _matches_wildcard(cfg["resource"], resource):
            continue
        matched.append(rule)
    return matched


def _matches_wildcard(pattern: str, value: str) -> bool:
    return pattern == "*" or pattern == value
