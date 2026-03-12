"""GET /v1/support/* — support context sidebar and SLA definitions."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_org
from app.database import get_db
from app.models import (
    ActiveRunCache,
    AgentMetricSnapshot,
    AlertFiring,
    AlertRule,
    AuditRun,
    CircuitBreakerEvent,
    Organization,
)
from app.schemas import (
    AgentStatusRow,
    AlertStatusSummary,
    AuditStatusSummary,
    CBStatusSummary,
    SLADefinition,
    SupportContext,
    SupportMetricsSummary,
    UpdateTierRequest,
)

router = APIRouter(prefix="/v1/support", tags=["support"])

# ---------------------------------------------------------------------------
# SLA response time matrix
# ---------------------------------------------------------------------------

_VALID_TIERS = {"free", "pro", "enterprise"}

_SLA: dict[str, SLADefinition] = {
    "free": SLADefinition(
        tier="free",
        p1_response_hours=None,
        p2_response_hours=None,
        p3_response_hours=None,
        p1_coverage="none",
        p2_coverage="none",
        p3_coverage="none",
        max_contacts=None,
    ),
    "pro": SLADefinition(
        tier="pro",
        p1_response_hours=4,
        p2_response_hours=8,
        p3_response_hours=24,
        p1_coverage="business_hours",
        p2_coverage="business_hours",
        p3_coverage="business_hours",
        max_contacts=3,
    ),
    "enterprise": SLADefinition(
        tier="enterprise",
        p1_response_hours=1,
        p2_response_hours=4,
        p3_response_hours=24,
        p1_coverage="24/7",
        p2_coverage="business_hours",
        p3_coverage="business_hours",
        max_contacts=None,
    ),
}


# ---------------------------------------------------------------------------
# SLA definition
# ---------------------------------------------------------------------------


@router.get("/sla", response_model=SLADefinition)
async def get_sla(
    org: Organization = Depends(get_current_org),
) -> SLADefinition:
    """Return the SLA definition for the authenticated org's current tier."""
    return _SLA.get(org.tier, _SLA["free"])


# ---------------------------------------------------------------------------
# Support context (sidebar widget)
# ---------------------------------------------------------------------------


@router.get("/context", response_model=SupportContext)
async def get_support_context(
    period_hours: int = Query(24, ge=1, le=168),
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> SupportContext:
    """
    Rich operational snapshot for the support sidebar widget.
    Returns the last N hours of fleet data for the authenticated org.
    Support engineers embed this in their ticket view to arrive pre-informed.
    """
    now = datetime.utcnow()
    since = now - timedelta(hours=period_hours)

    metrics = await _build_metrics(org.id, since, now, db)
    cb_status = await _build_cb_status(org.id, since, now, db)
    alert_status = await _build_alert_status(org.id, since, now, db)
    audit_status = await _build_audit_status(org.id, since, now, db)
    agents = await _build_agents(org.id, since, now, db)

    return SupportContext(
        org_id=org.id,
        org_name=org.name,
        tier=org.tier,
        sla=_SLA.get(org.tier, _SLA["free"]),
        period_hours=period_hours,
        metrics=metrics,
        circuit_breaker=cb_status,
        alerts=alert_status,
        audit=audit_status,
        agents=agents,
        generated_at=now,
    )


# ---------------------------------------------------------------------------
# Tier management (internal — no separate admin auth in v1)
# ---------------------------------------------------------------------------


@router.patch("/tier", response_model=dict)
async def update_tier(
    body: UpdateTierRequest,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Update the org's support tier and plan metadata.
    In v1 this is authenticated by the org's own API key.
    Production should gate this behind an internal admin token.
    """
    if body.tier not in _VALID_TIERS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid tier. Must be one of: {sorted(_VALID_TIERS)}",
        )
    org.tier = body.tier
    if body.plan_metadata:
        org.plan_metadata = {**(org.plan_metadata or {}), **body.plan_metadata}
    await db.commit()
    await db.refresh(org)
    return {
        "org_id": org.id,
        "tier": org.tier,
        "plan_metadata": org.plan_metadata,
        "sla": _SLA.get(org.tier, _SLA["free"]).model_dump(),
    }


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


async def _build_metrics(
    org_id: str, since: datetime, now: datetime, db: AsyncSession
) -> SupportMetricsSummary:
    result = await db.execute(
        select(AgentMetricSnapshot).where(
            AgentMetricSnapshot.org_id == org_id,
            AgentMetricSnapshot.bucket >= since,
            AgentMetricSnapshot.bucket <= now,
        )
    )
    snaps = list(result.scalars().all())

    total = sum(s.runs_total for s in snaps)
    success = sum(s.runs_success for s in snaps)
    errors = sum(s.runs_error for s in snaps)
    cost = sum(s.cost_usd for s in snaps)
    input_tok = sum(s.input_tokens for s in snaps)
    output_tok = sum(s.output_tokens for s in snaps)
    agents = {(s.agent_name, s.project) for s in snaps}

    stale_cutoff = now - timedelta(hours=1)
    active_result = await db.execute(
        select(ActiveRunCache).where(
            ActiveRunCache.org_id == org_id,
            ActiveRunCache.started_at >= stale_cutoff,
        )
    )
    active_count = len(list(active_result.scalars().all()))

    return SupportMetricsSummary(
        total_runs=total,
        runs_success=success,
        runs_error=errors,
        error_rate_pct=round(errors / total * 100, 2) if total > 0 else 0.0,
        total_cost_usd=round(cost, 6),
        total_input_tokens=input_tok,
        total_output_tokens=output_tok,
        active_runs=active_count,
        agents_seen=len(agents),
    )


async def _build_cb_status(
    org_id: str, since: datetime, now: datetime, db: AsyncSession
) -> CBStatusSummary:
    result = await db.execute(
        select(CircuitBreakerEvent).where(
            CircuitBreakerEvent.org_id == org_id,
            CircuitBreakerEvent.occurred_at >= since,
        ).order_by(CircuitBreakerEvent.occurred_at.desc())
    )
    events = list(result.scalars().all())

    # Determine which agents are currently "open" (most recent state per agent)
    latest_state: dict[str, str] = {}
    for ev in reversed(events):
        latest_state[ev.agent_name] = ev.new_state

    open_agents = [ag for ag, state in latest_state.items() if state == "open"]

    recent = [
        {
            "agent_name": ev.agent_name,
            "resource": ev.resource,
            "prev_state": ev.prev_state,
            "new_state": ev.new_state,
            "failure_count": ev.failure_count,
            "occurred_at": ev.occurred_at.isoformat(),
        }
        for ev in events[:10]  # last 10 events
    ]

    return CBStatusSummary(open_agents=open_agents, recent_events=recent)


async def _build_alert_status(
    org_id: str, since: datetime, now: datetime, db: AsyncSession
) -> AlertStatusSummary:
    # Currently firing alerts
    firing_result = await db.execute(
        select(AlertFiring).where(
            AlertFiring.org_id == org_id,
            AlertFiring.state == "firing",
        )
    )
    firing_rows = list(firing_result.scalars().all())

    # Recent firings in period (all states)
    recent_result = await db.execute(
        select(AlertFiring).where(
            AlertFiring.org_id == org_id,
            AlertFiring.fired_at >= since,
        ).order_by(AlertFiring.fired_at.desc()).limit(10)
    )
    recent_rows = list(recent_result.scalars().all())

    # Enrich with rule names
    rule_ids = {r.rule_id for r in recent_rows}
    rules: dict[str, str] = {}
    for rid in rule_ids:
        rule_result = await db.execute(select(AlertRule).where(AlertRule.id == rid))
        rule = rule_result.scalar_one_or_none()
        if rule:
            rules[rid] = rule.name

    recent = [
        {
            "id": r.id,
            "rule_name": rules.get(r.rule_id, r.rule_id),
            "state": r.state,
            "fired_at": r.fired_at.isoformat(),
            "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
            "context": r.context,
        }
        for r in recent_rows
    ]

    return AlertStatusSummary(firing_count=len(firing_rows), recent_firings=recent)


async def _build_audit_status(
    org_id: str, since: datetime, now: datetime, db: AsyncSession
) -> AuditStatusSummary:
    result = await db.execute(
        select(AuditRun).where(
            AuditRun.org_id == org_id,
            AuditRun.created_at >= since,
        )
    )
    runs = list(result.scalars().all())

    return AuditStatusSummary(
        total_runs=len(runs),
        verified_runs=sum(1 for r in runs if r.integrity == "verified"),
        failed_runs=sum(1 for r in runs if r.integrity == "failed"),
        pending_runs=sum(1 for r in runs if r.integrity == "pending"),
    )


async def _build_agents(
    org_id: str, since: datetime, now: datetime, db: AsyncSession
) -> list[AgentStatusRow]:
    result = await db.execute(
        select(AgentMetricSnapshot).where(
            AgentMetricSnapshot.org_id == org_id,
            AgentMetricSnapshot.bucket >= since,
        )
    )
    snaps = list(result.scalars().all())

    agg: dict[tuple[str, str], dict] = defaultdict(lambda: {
        "runs_total": 0, "runs_error": 0, "cost_usd": 0.0, "last_bucket": None,
    })
    for s in snaps:
        key = (s.agent_name, s.project)
        a = agg[key]
        a["runs_total"] += s.runs_total
        a["runs_error"] += s.runs_error
        a["cost_usd"] += s.cost_usd
        if a["last_bucket"] is None or s.bucket > a["last_bucket"]:
            a["last_bucket"] = s.bucket

    # Latest CB state per agent
    cb_result = await db.execute(
        select(CircuitBreakerEvent)
        .where(CircuitBreakerEvent.org_id == org_id)
        .order_by(CircuitBreakerEvent.occurred_at.desc())
    )
    cb_events = list(cb_result.scalars().all())
    latest_cb: dict[str, str] = {}
    for ev in reversed(cb_events):
        latest_cb[ev.agent_name] = ev.new_state

    rows = []
    for (agent_name, project), a in sorted(agg.items()):
        n = a["runs_total"]
        rows.append(AgentStatusRow(
            agent_name=agent_name,
            project=project,
            runs_total=n,
            error_rate_pct=round(a["runs_error"] / n * 100, 2) if n > 0 else 0.0,
            total_cost_usd=round(a["cost_usd"], 6),
            circuit_breaker_state=latest_cb.get(agent_name, "closed"),
            last_seen=a["last_bucket"],
        ))
    return rows
