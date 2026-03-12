"""Alert notification dispatch — Slack, PagerDuty, webhook, email."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from app.models import AlertChannel, AlertFiring, AlertRule

logger = logging.getLogger("agentkit.cloud.alerts")

_PAGERDUTY_URL = "https://events.pagerduty.com/v2/enqueue"


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


async def dispatch_alert(
    rule: AlertRule,
    firing: AlertFiring,
    event: str,
    db: Any,
) -> None:
    """Send notification to all channels attached to the rule."""
    from sqlalchemy import select
    from app.models import AlertChannel

    channel_ids: list[str] = rule.channel_ids or []
    if not channel_ids:
        return

    payload = _build_payload(rule, firing, event)

    for ch_id in channel_ids:
        result = await db.execute(select(AlertChannel).where(AlertChannel.id == ch_id))
        channel = result.scalar_one_or_none()
        if channel is None:
            continue
        try:
            await send_to_channel(channel, event, payload)
            firing.notifications_sent += 1
        except Exception as exc:
            logger.warning("Channel %s (%s) dispatch failed: %s", channel.id, channel.type, exc)


async def send_to_channel(channel: AlertChannel, event: str, payload: dict) -> None:
    """Dispatch a single notification to a channel. May raise on failure."""
    if channel.type == "slack":
        await _send_slack(channel.config, event, payload)
    elif channel.type == "pagerduty":
        await _send_pagerduty(channel.config, event, payload)
    elif channel.type == "webhook":
        await _send_webhook(channel.config, event, payload)
    elif channel.type == "email":
        _log_email(channel.config, event, payload)


async def send_test_notification(channel: AlertChannel) -> None:
    """Send a test notification on channel creation. May raise on failure."""
    payload = {
        "event": "alert.test",
        "rule_name": "Test Notification",
        "type": "test",
        "agent_name": "*",
        "project": "*",
        "fired_at": datetime.utcnow().isoformat(),
        "context": {"message": "This is a test notification from agent-kit Cloud."},
    }
    await send_to_channel(channel, "alert.test", payload)


# ---------------------------------------------------------------------------
# Per-channel implementations
# ---------------------------------------------------------------------------


async def _send_slack(config: dict, event: str, payload: dict) -> None:
    webhook_url = config.get("webhook_url", "")
    if not webhook_url:
        raise ValueError("Slack channel missing webhook_url")

    color = "#d9534f" if event == "alert.firing" else "#5cb85c"
    status_label = "ALERT" if event == "alert.firing" else ("RESOLVED" if event == "alert.resolved" else "TEST")

    body = {
        "attachments": [{
            "color": color,
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*[agent-kit] {status_label}: {payload.get('rule_name', 'Alert')}*",
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Type:* `{payload.get('type', '')}`"},
                        {"type": "mrkdwn", "text": f"*Agent:* `{payload.get('agent_name', '*')}`"},
                        {"type": "mrkdwn", "text": f"*Project:* `{payload.get('project', '*')}`"},
                    ],
                },
            ],
        }],
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(webhook_url, json=body)
        resp.raise_for_status()


async def _send_pagerduty(config: dict, event: str, payload: dict) -> None:
    routing_key = config.get("routing_key", "")
    if not routing_key:
        raise ValueError("PagerDuty channel missing routing_key")

    severity = config.get("severity", "error")
    dedup_key = payload.get("alert_id", payload.get("rule_name", ""))

    if event == "alert.resolved":
        body: dict[str, Any] = {
            "routing_key": routing_key,
            "dedup_key": dedup_key,
            "event_action": "resolve",
        }
    else:
        body = {
            "routing_key": routing_key,
            "dedup_key": dedup_key,
            "event_action": "trigger",
            "payload": {
                "summary": f"[agent-kit] {payload.get('rule_name', '')} — {payload.get('type', '')}",
                "severity": severity,
                "source": "agent-kit",
                "custom_details": payload,
            },
        }

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(_PAGERDUTY_URL, json=body)
        resp.raise_for_status()


async def _send_webhook(config: dict, event: str, payload: dict) -> None:
    url = config.get("url", "")
    if not url:
        raise ValueError("Webhook channel missing url")

    secret: str | None = config.get("secret")
    extra_headers: dict = config.get("headers", {})

    body_bytes = json.dumps(payload).encode()
    headers: dict[str, str] = {"Content-Type": "application/json", **extra_headers}

    if secret:
        sig = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
        headers["X-AgentKit-Signature"] = f"sha256={sig}"

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, content=body_bytes, headers=headers)
                resp.raise_for_status()
            return
        except Exception as exc:
            if attempt == 2:
                raise
            await asyncio.sleep(2.0 ** attempt)


def _log_email(config: dict, event: str, payload: dict) -> None:
    """Log-only email delivery (no SMTP configured in dev)."""
    to: list[str] = config.get("to", [])
    agent = payload.get("agent_name", "*")
    alert_type = payload.get("type", "")
    rule_name = payload.get("rule_name", "")
    status = "ALERT" if event == "alert.firing" else ("RESOLVED" if event == "alert.resolved" else "TEST")
    logger.info(
        "Email [%s] to=%s subject='[agent-kit] %s: %s — %s (%s)'",
        event, to, status, alert_type, rule_name, agent,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_payload(rule: AlertRule, firing: AlertFiring, event: str) -> dict:
    return {
        "event": event,
        "alert_id": firing.id,
        "rule_name": rule.name,
        "type": rule.type,
        "agent_name": rule.config.get("agent_name", "*"),
        "project": rule.config.get("project", "*"),
        "fired_at": firing.fired_at.isoformat() if firing.fired_at else "",
        "context": firing.context or {},
    }
