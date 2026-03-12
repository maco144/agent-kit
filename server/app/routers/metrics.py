"""GET /v1/metrics/* — fleet dashboard API."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_org
from app.database import get_db
from app.models import ActiveRunCache, AgentMetricSnapshot, CircuitBreakerEvent, Organization
from app.schemas import (
    ActiveRunDetail,
    ActiveRunsResponse,
    AgentSummary,
    AgentsResponse,
    CBAgentDetail,
    CBEventDetail,
    CircuitBreakerResponse,
    CostDataPoint,
    CostResponse,
    CostSeries,
    MetricsSummary,
    RunsDataPoint,
    RunsResponse,
    RunsSeries,
)

router = APIRouter(prefix="/v1/metrics", tags=["metrics"])

_DEFAULT_WINDOW_HOURS = 24


def _parse_window(
    from_: datetime | None,
    to: datetime | None,
) -> tuple[datetime, datetime]:
    now = datetime.utcnow()
    end = to or now
    start = from_ or (now - timedelta(hours=_DEFAULT_WINDOW_HOURS))
    return start, end


def _auto_resolution(start: datetime, end: datetime) -> str:
    window_hours = (end - start).total_seconds() / 3600
    if window_hours <= 2:
        return "1m"
    if window_hours <= 72:
        return "1h"
    return "1d"


def _truncate_to_resolution(dt: datetime, resolution: str) -> datetime:
    if resolution == "1m":
        return dt.replace(second=0, microsecond=0)
    if resolution == "1h":
        return dt.replace(minute=0, second=0, microsecond=0)
    # 1d
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


@router.get("/summary", response_model=MetricsSummary)
async def get_summary(
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    project: str | None = Query(None),
    agent_name: str | None = Query(None),
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> MetricsSummary:
    start, end = _parse_window(from_, to)

    q = select(AgentMetricSnapshot).where(
        AgentMetricSnapshot.org_id == org.id,
        AgentMetricSnapshot.bucket >= start,
        AgentMetricSnapshot.bucket <= end,
    )
    if project:
        q = q.where(AgentMetricSnapshot.project == project)
    if agent_name:
        q = q.where(AgentMetricSnapshot.agent_name == agent_name)

    result = await db.execute(q)
    rows = list(result.scalars().all())

    total_runs = sum(r.runs_total for r in rows)
    runs_success = sum(r.runs_success for r in rows)
    runs_error = sum(r.runs_error for r in rows)
    total_cost = sum(r.cost_usd for r in rows)
    total_input = sum(r.input_tokens for r in rows)
    total_output = sum(r.output_tokens for r in rows)
    error_rate = round(runs_error / total_runs * 100, 2) if total_runs > 0 else 0.0
    agents = {(r.agent_name, r.project) for r in rows}
    projects = sorted({r.project for r in rows})

    # Active runs (exclude stale > 1 hour)
    stale_cutoff = datetime.utcnow() - timedelta(hours=1)
    active_q = select(func.count()).select_from(
        select(ActiveRunCache).where(
            ActiveRunCache.org_id == org.id,
            ActiveRunCache.started_at >= stale_cutoff,
        ).subquery()
    )
    active_count_result = await db.execute(active_q)
    active_count = active_count_result.scalar_one()

    return MetricsSummary(
        window={"from": start.isoformat(), "to": end.isoformat()},
        total_runs=total_runs,
        runs_success=runs_success,
        runs_error=runs_error,
        error_rate_pct=error_rate,
        total_cost_usd=round(total_cost, 6),
        total_input_tokens=total_input,
        total_output_tokens=total_output,
        active_runs=active_count,
        agents_count=len(agents),
        projects=projects,
    )


# ---------------------------------------------------------------------------
# Cost time-series
# ---------------------------------------------------------------------------


@router.get("/cost", response_model=CostResponse)
async def get_cost(
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    resolution: str | None = Query(None, pattern="^(1m|1h|1d)$"),
    project: str | None = Query(None),
    agent_name: str | None = Query(None),
    group_by: str = Query("agent_name", pattern="^(agent_name|model|project)$"),
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> CostResponse:
    start, end = _parse_window(from_, to)
    res = resolution or _auto_resolution(start, end)

    q = select(AgentMetricSnapshot).where(
        AgentMetricSnapshot.org_id == org.id,
        AgentMetricSnapshot.bucket >= start,
        AgentMetricSnapshot.bucket <= end,
    )
    if project:
        q = q.where(AgentMetricSnapshot.project == project)
    if agent_name:
        q = q.where(AgentMetricSnapshot.agent_name == agent_name)

    result = await db.execute(q)
    rows = list(result.scalars().all())

    # Group rows by series label then by bucket
    series_data: dict[tuple[str, str], dict[datetime, dict[str, Any]]] = defaultdict(
        lambda: defaultdict(lambda: {"cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0})
    )

    for row in rows:
        if group_by == "agent_name":
            label, proj = row.agent_name, row.project
        elif group_by == "model":
            label, proj = row.model, row.project
        else:
            label, proj = row.project, row.project

        bucket = _truncate_to_resolution(row.bucket, res)
        series_data[(label, proj)][bucket]["cost_usd"] += row.cost_usd
        series_data[(label, proj)][bucket]["input_tokens"] += row.input_tokens
        series_data[(label, proj)][bucket]["output_tokens"] += row.output_tokens

    series = []
    for (label, proj), buckets in sorted(series_data.items()):
        data_points = [
            CostDataPoint(
                bucket=b,
                cost_usd=round(v["cost_usd"], 6),
                input_tokens=v["input_tokens"],
                output_tokens=v["output_tokens"],
            )
            for b, v in sorted(buckets.items())
        ]
        total = sum(dp.cost_usd for dp in data_points)
        series.append(CostSeries(label=label, project=proj, data=data_points, total_cost_usd=round(total, 6)))

    return CostResponse(group_by=group_by, resolution=res, series=series)


# ---------------------------------------------------------------------------
# Runs time-series
# ---------------------------------------------------------------------------


@router.get("/runs", response_model=RunsResponse)
async def get_runs(
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    resolution: str | None = Query(None, pattern="^(1m|1h|1d)$"),
    project: str | None = Query(None),
    agent_name: str | None = Query(None),
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> RunsResponse:
    start, end = _parse_window(from_, to)
    res = resolution or _auto_resolution(start, end)

    q = select(AgentMetricSnapshot).where(
        AgentMetricSnapshot.org_id == org.id,
        AgentMetricSnapshot.bucket >= start,
        AgentMetricSnapshot.bucket <= end,
    )
    if project:
        q = q.where(AgentMetricSnapshot.project == project)
    if agent_name:
        q = q.where(AgentMetricSnapshot.agent_name == agent_name)

    result = await db.execute(q)
    rows = list(result.scalars().all())

    # Group by agent_name, then bucket
    series_data: dict[str, dict[datetime, dict[str, Any]]] = defaultdict(
        lambda: defaultdict(lambda: {
            "runs_total": 0, "runs_success": 0, "runs_error": 0,
            "total_turns": 0, "total_duration_ms": 0,
        })
    )

    for row in rows:
        bucket = _truncate_to_resolution(row.bucket, res)
        d = series_data[row.agent_name][bucket]
        d["runs_total"] += row.runs_total
        d["runs_success"] += row.runs_success
        d["runs_error"] += row.runs_error
        d["total_turns"] += row.total_turns
        d["total_duration_ms"] += row.total_duration_ms

    series = []
    for label, buckets in sorted(series_data.items()):
        data_points = []
        for b, v in sorted(buckets.items()):
            n = v["runs_total"]
            data_points.append(RunsDataPoint(
                bucket=b,
                runs_total=n,
                runs_success=v["runs_success"],
                runs_error=v["runs_error"],
                avg_turns=round(v["total_turns"] / n, 2) if n > 0 else 0.0,
                avg_duration_ms=v["total_duration_ms"] // n if n > 0 else 0,
            ))
        series.append(RunsSeries(label=label, data=data_points))

    return RunsResponse(resolution=res, series=series)


# ---------------------------------------------------------------------------
# Agents list
# ---------------------------------------------------------------------------


@router.get("/agents", response_model=AgentsResponse)
async def get_agents(
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    project: str | None = Query(None),
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> AgentsResponse:
    start, end = _parse_window(from_, to)

    q = select(AgentMetricSnapshot).where(
        AgentMetricSnapshot.org_id == org.id,
        AgentMetricSnapshot.bucket >= start,
        AgentMetricSnapshot.bucket <= end,
    )
    if project:
        q = q.where(AgentMetricSnapshot.project == project)

    result = await db.execute(q)
    rows = list(result.scalars().all())

    # Aggregate by (agent_name, project)
    agg: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {
        "models": set(),
        "runs_total": 0,
        "runs_error": 0,
        "cost_usd": 0.0,
        "total_turns": 0,
        "last_bucket": None,
    })

    for row in rows:
        key = (row.agent_name, row.project)
        a = agg[key]
        if row.model:
            a["models"].add(row.model)
        a["runs_total"] += row.runs_total
        a["runs_error"] += row.runs_error
        a["cost_usd"] += row.cost_usd
        a["total_turns"] += row.total_turns
        if a["last_bucket"] is None or row.bucket > a["last_bucket"]:
            a["last_bucket"] = row.bucket

    # Get most recent CB state per agent
    cb_result = await db.execute(
        select(CircuitBreakerEvent)
        .where(CircuitBreakerEvent.org_id == org.id)
        .order_by(CircuitBreakerEvent.occurred_at.desc())
    )
    cb_events = list(cb_result.scalars().all())
    latest_cb: dict[str, str] = {}  # agent_name -> current state
    for ev in reversed(cb_events):
        latest_cb[ev.agent_name] = ev.new_state

    agents = []
    for (agent_name, proj), a in sorted(agg.items()):
        n = a["runs_total"]
        cost = a["cost_usd"]
        error_rate = round(a["runs_error"] / n * 100, 2) if n > 0 else 0.0
        agents.append(AgentSummary(
            agent_name=agent_name,
            project=proj,
            models_used=sorted(a["models"]),
            runs_total=n,
            error_rate_pct=error_rate,
            total_cost_usd=round(cost, 6),
            avg_cost_per_run_usd=round(cost / n, 6) if n > 0 else 0.0,
            avg_turns=round(a["total_turns"] / n, 2) if n > 0 else 0.0,
            circuit_breaker_state=latest_cb.get(agent_name, "closed"),
            last_seen=a["last_bucket"],
        ))

    return AgentsResponse(agents=agents)


# ---------------------------------------------------------------------------
# Circuit breaker history
# ---------------------------------------------------------------------------


@router.get("/circuit-breaker", response_model=CircuitBreakerResponse)
async def get_circuit_breaker(
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    project: str | None = Query(None),
    agent_name: str | None = Query(None),
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> CircuitBreakerResponse:
    start, end = _parse_window(from_, to)

    q = select(CircuitBreakerEvent).where(
        CircuitBreakerEvent.org_id == org.id,
        CircuitBreakerEvent.occurred_at >= start,
        CircuitBreakerEvent.occurred_at <= end,
    )
    if project:
        q = q.where(CircuitBreakerEvent.project == project)
    if agent_name:
        q = q.where(CircuitBreakerEvent.agent_name == agent_name)
    q = q.order_by(CircuitBreakerEvent.agent_name, CircuitBreakerEvent.occurred_at)

    result = await db.execute(q)
    all_events = list(result.scalars().all())

    # Group by (agent_name, resource)
    groups: dict[tuple[str, str], list[CircuitBreakerEvent]] = defaultdict(list)
    for ev in all_events:
        groups[(ev.agent_name, ev.resource)].append(ev)

    agents = []
    for (ag_name, resource), events in sorted(groups.items()):
        current_state = events[-1].new_state if events else "closed"
        event_details: list[CBEventDetail] = []
        for i, ev in enumerate(events):
            duration_open_ms: int | None = None
            if ev.new_state == "open":
                # Find the next transition (closed or half_open) to compute duration
                for next_ev in events[i + 1:]:
                    if next_ev.new_state in ("closed", "half_open"):
                        duration_open_ms = int(
                            (next_ev.occurred_at - ev.occurred_at).total_seconds() * 1000
                        )
                        break
            event_details.append(CBEventDetail(
                prev_state=ev.prev_state,
                new_state=ev.new_state,
                failure_count=ev.failure_count,
                occurred_at=ev.occurred_at,
                duration_open_ms=duration_open_ms,
            ))
        agents.append(CBAgentDetail(
            agent_name=ag_name,
            resource=resource,
            current_state=current_state,
            events=event_details,
        ))

    return CircuitBreakerResponse(agents=agents)


# ---------------------------------------------------------------------------
# Active runs (live view)
# ---------------------------------------------------------------------------


@router.get("/active", response_model=ActiveRunsResponse)
async def get_active_runs(
    project: str | None = Query(None),
    agent_name: str | None = Query(None),
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> ActiveRunsResponse:
    stale_cutoff = datetime.utcnow() - timedelta(hours=1)
    q = select(ActiveRunCache).where(
        ActiveRunCache.org_id == org.id,
        ActiveRunCache.started_at >= stale_cutoff,
    )
    if project:
        q = q.where(ActiveRunCache.project == project)
    if agent_name:
        q = q.where(ActiveRunCache.agent_name == agent_name)
    q = q.order_by(ActiveRunCache.started_at.desc())

    result = await db.execute(q)
    rows = list(result.scalars().all())
    now = datetime.utcnow()

    active_runs = [
        ActiveRunDetail(
            run_id=r.run_id,
            agent_name=r.agent_name,
            project=r.project,
            model=r.model,
            started_at=r.started_at,
            elapsed_ms=int((now - r.started_at).total_seconds() * 1000),
            turns_so_far=r.turns_so_far,
            cost_so_far_usd=r.cost_so_far_usd,
            tokens_so_far=r.input_tokens + r.output_tokens,
        )
        for r in rows
    ]

    return ActiveRunsResponse(active_runs=active_runs, count=len(active_runs))
