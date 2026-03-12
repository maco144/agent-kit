"""GET|POST|PATCH|DELETE /v1/alerts/* — rule and channel management, firing history."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_org
from app.database import get_db
from app.models import AlertChannel, AlertFiring, AlertRule, Organization
from app.schemas import (
    AckFiringRequest,
    AlertChannelSchema,
    AlertFiringSchema,
    AlertRuleSchema,
    CreateChannelRequest,
    CreateChannelResponse,
    CreateRuleRequest,
    UpdateRuleRequest,
)

router = APIRouter(prefix="/v1/alerts", tags=["alerts"])

_VALID_CHANNEL_TYPES = {"email", "slack", "pagerduty", "webhook"}
_VALID_RULE_TYPES = {
    "circuit_breaker_open", "cost_anomaly", "error_rate", "audit_integrity_failure"
}


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


@router.get("/channels")
async def list_channels(
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(AlertChannel)
        .where(AlertChannel.org_id == org.id)
        .order_by(AlertChannel.created_at)
    )
    rows = list(result.scalars().all())
    return {"channels": [AlertChannelSchema.model_validate(r) for r in rows]}


@router.post("/channels", status_code=status.HTTP_201_CREATED)
async def create_channel(
    body: CreateChannelRequest,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> CreateChannelResponse:
    if body.type not in _VALID_CHANNEL_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid channel type. Must be one of: {sorted(_VALID_CHANNEL_TYPES)}",
        )

    channel = AlertChannel(
        org_id=org.id,
        name=body.name,
        type=body.type,
        config=body.config,
    )
    db.add(channel)
    await db.commit()
    await db.refresh(channel)

    # Attempt test notification
    test_sent = False
    try:
        from app.alerting.dispatch import send_test_notification
        await send_test_notification(channel)
        test_sent = True
    except Exception as exc:
        import logging
        logging.getLogger("agentkit.cloud.alerts").warning(
            "Test notification failed for channel %s: %s", channel.id, exc
        )

    return CreateChannelResponse(
        channel=AlertChannelSchema.model_validate(channel),
        test_sent=test_sent,
    )


@router.post("/channels/{channel_id}/test", status_code=status.HTTP_200_OK)
async def test_channel(
    channel_id: str,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> dict:
    channel = await _get_channel_or_404(channel_id, org.id, db)
    try:
        from app.alerting.dispatch import send_test_notification
        await send_test_notification(channel)
        return {"sent": True}
    except Exception as exc:
        return {"sent": False, "error": str(exc)}


@router.delete("/channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(
    channel_id: str,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> None:
    channel = await _get_channel_or_404(channel_id, org.id, db)
    await db.delete(channel)
    await db.commit()


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


@router.get("/rules")
async def list_rules(
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(AlertRule)
        .where(AlertRule.org_id == org.id)
        .order_by(AlertRule.created_at)
    )
    rows = list(result.scalars().all())
    return {"rules": [AlertRuleSchema.model_validate(r) for r in rows]}


@router.post("/rules", status_code=status.HTTP_201_CREATED)
async def create_rule(
    body: CreateRuleRequest,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> AlertRuleSchema:
    if body.type not in _VALID_RULE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid rule type. Must be one of: {sorted(_VALID_RULE_TYPES)}",
        )

    # Verify referenced channels exist
    for ch_id in body.channel_ids:
        ch = await _get_channel_or_404(ch_id, org.id, db)
        _ = ch  # validate exists

    rule = AlertRule(
        org_id=org.id,
        name=body.name,
        type=body.type,
        config=body.config,
        enabled=body.enabled,
        channel_ids=body.channel_ids,
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return AlertRuleSchema.model_validate(rule)


@router.get("/rules/{rule_id}")
async def get_rule(
    rule_id: str,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> AlertRuleSchema:
    rule = await _get_rule_or_404(rule_id, org.id, db)
    return AlertRuleSchema.model_validate(rule)


@router.patch("/rules/{rule_id}")
async def update_rule(
    rule_id: str,
    body: UpdateRuleRequest,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> AlertRuleSchema:
    rule = await _get_rule_or_404(rule_id, org.id, db)

    if body.name is not None:
        rule.name = body.name
    if body.config is not None:
        rule.config = body.config
    if body.enabled is not None:
        rule.enabled = body.enabled
    if body.channel_ids is not None:
        for ch_id in body.channel_ids:
            await _get_channel_or_404(ch_id, org.id, db)
        rule.channel_ids = body.channel_ids
    if body.muted_until is not None:
        rule.muted_until = body.muted_until
    rule.updated_at = datetime.utcnow()

    await db.commit()
    await db.refresh(rule)
    return AlertRuleSchema.model_validate(rule)


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(
    rule_id: str,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> None:
    rule = await _get_rule_or_404(rule_id, org.id, db)
    # Cascade: delete all firings for this rule
    firings_result = await db.execute(
        select(AlertFiring).where(AlertFiring.rule_id == rule.id)
    )
    for firing in firings_result.scalars().all():
        await db.delete(firing)
    await db.delete(rule)
    await db.commit()


# ---------------------------------------------------------------------------
# Firings
# ---------------------------------------------------------------------------


@router.get("/firing")
async def list_firings(
    state: str | None = Query(None, pattern="^(firing|resolved|acked)$"),
    rule_id: str | None = Query(None),
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> dict:
    q = select(AlertFiring).where(AlertFiring.org_id == org.id)
    if state:
        q = q.where(AlertFiring.state == state)
    if rule_id:
        q = q.where(AlertFiring.rule_id == rule_id)
    if from_:
        q = q.where(AlertFiring.fired_at >= from_)
    if to:
        q = q.where(AlertFiring.fired_at <= to)
    q = q.order_by(AlertFiring.fired_at.desc()).limit(limit)

    result = await db.execute(q)
    rows = list(result.scalars().all())
    return {"firings": [AlertFiringSchema.model_validate(r) for r in rows]}


@router.post("/firing/{firing_id}/ack", status_code=status.HTTP_200_OK)
async def ack_firing(
    firing_id: str,
    body: AckFiringRequest,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
) -> AlertFiringSchema:
    result = await db.execute(
        select(AlertFiring).where(
            AlertFiring.id == firing_id,
            AlertFiring.org_id == org.id,
        )
    )
    firing = result.scalar_one_or_none()
    if firing is None:
        raise HTTPException(status_code=404, detail=f"Firing '{firing_id}' not found.")
    if firing.state != "firing":
        raise HTTPException(
            status_code=409,
            detail=f"Firing is already '{firing.state}', cannot ack.",
        )

    firing.state = "acked"
    firing.acked_at = datetime.utcnow()
    if body.comment:
        ctx = dict(firing.context or {})
        ctx["ack_comment"] = body.comment
        firing.context = ctx
    await db.commit()
    await db.refresh(firing)
    return AlertFiringSchema.model_validate(firing)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_channel_or_404(channel_id: str, org_id: str, db: AsyncSession) -> AlertChannel:
    result = await db.execute(
        select(AlertChannel).where(
            AlertChannel.id == channel_id,
            AlertChannel.org_id == org_id,
        )
    )
    channel = result.scalar_one_or_none()
    if channel is None:
        raise HTTPException(status_code=404, detail=f"Channel '{channel_id}' not found.")
    return channel


async def _get_rule_or_404(rule_id: str, org_id: str, db: AsyncSession) -> AlertRule:
    result = await db.execute(
        select(AlertRule).where(
            AlertRule.id == rule_id,
            AlertRule.org_id == org_id,
        )
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=404, detail=f"Rule '{rule_id}' not found.")
    return rule
