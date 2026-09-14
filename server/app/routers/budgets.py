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
