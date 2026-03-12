"""SQLAlchemy ORM models for agent-kit Cloud."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.utcnow()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    tier: Mapped[str] = mapped_column(
        String(16), nullable=False, default="free"
    )  # free | pro | enterprise
    plan_metadata: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )  # CSE name, slack channel, custom SLA terms, etc.
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    api_keys: Mapped[list[ApiKey]] = relationship("ApiKey", back_populates="organization")
    audit_runs: Mapped[list[AuditRun]] = relationship("AuditRun", back_populates="organization")


class ApiKey(Base):
    """
    Stores a SHA-256 hash of the API key (never the key itself).
    The prefix (first 12 chars) is stored plaintext for identification.
    """
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(String(36), ForeignKey("organizations.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)  # e.g. "akt_live_a3f9"
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    organization: Mapped[Organization] = relationship("Organization", back_populates="api_keys")

    __table_args__ = (Index("ix_api_keys_key_hash", "key_hash"),)


# ---------------------------------------------------------------------------
# Audit Trail
# ---------------------------------------------------------------------------


class AuditRun(Base):
    """One record per Agent.run() call that flushed audit events."""
    __tablename__ = "audit_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(String(36), ForeignKey("organizations.id"), nullable=False)
    project: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(255), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    genesis_root: Mapped[str] = mapped_column(String(64), nullable=False, default="0" * 64)
    final_root_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    integrity: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )  # verified | failed | pending
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    organization: Mapped[Organization] = relationship("Organization", back_populates="audit_runs")
    events: Mapped[list[AuditEvent]] = relationship(
        "AuditEvent", back_populates="run", order_by="AuditEvent.seq"
    )

    __table_args__ = (
        Index("ix_audit_runs_org_id", "org_id"),
        Index("ix_audit_runs_org_run_id", "org_id", "run_id"),
    )


class AuditEvent(Base):
    """One record per AuditEventRecord in the chain. Append-only."""
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("audit_runs.run_id"), nullable=False)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False)  # denormalized
    event_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    prev_root: Mapped[str] = mapped_column(String(64), nullable=False)
    leaf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    run: Mapped[AuditRun] = relationship("AuditRun", back_populates="events")

    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_audit_events_run_seq"),
        Index("ix_audit_events_run_id", "run_id"),
        Index("ix_audit_events_org_event_type", "org_id", "event_type"),
        Index("ix_audit_events_leaf_hash", "leaf_hash"),
    )


# ---------------------------------------------------------------------------
# Fleet Dashboard — metrics pipeline tables
# ---------------------------------------------------------------------------


class ActiveRunCache(Base):
    """
    In-progress runs. Populated on run_start, updated on turn_complete,
    deleted on run_complete / run_error. TTL: rows older than 1 hour are
    considered stale (abandoned runs) and excluded from /v1/metrics/active.
    """
    __tablename__ = "active_run_cache"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False)
    project: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(255), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    turns_so_far: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_so_far_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    __table_args__ = (
        Index("ix_active_run_cache_org", "org_id"),
    )


class AgentMetricSnapshot(Base):
    """
    One-minute aggregation bucket per (org, project, agent_name, model).
    Runs are accumulated via upsert on run_complete / run_error.
    """
    __tablename__ = "agent_metric_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False)
    project: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(255), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    bucket: Mapped[datetime] = mapped_column(DateTime, nullable=False)  # truncated to minute
    runs_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    runs_success: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    runs_error: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_turns: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint(
            "org_id", "project", "agent_name", "model", "bucket",
            name="uq_metric_snapshot_key",
        ),
        Index("ix_metric_snapshots_org_bucket", "org_id", "bucket"),
        Index("ix_metric_snapshots_org_agent_bucket", "org_id", "project", "agent_name", "bucket"),
    )


class CircuitBreakerEvent(Base):
    """One record per circuit breaker state transition."""
    __tablename__ = "circuit_breaker_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False)
    project: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(255), nullable=False)
    resource: Mapped[str] = mapped_column(String(128), nullable=False)
    prev_state: Mapped[str] = mapped_column(String(16), nullable=False)
    new_state: Mapped[str] = mapped_column(String(16), nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_cb_events_org_agent_time", "org_id", "agent_name", "occurred_at"),
        Index("ix_cb_events_org_state", "org_id", "new_state", "occurred_at"),
    )


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------


class AlertChannel(Base):
    """A notification destination (Slack, PagerDuty, webhook, email)."""
    __tablename__ = "alert_channels"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)   # email|slack|pagerduty|webhook
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    __table_args__ = (
        Index("ix_alert_channels_org", "org_id"),
    )


class AlertRule(Base):
    """An alert rule: type + config + which channels to notify."""
    __tablename__ = "alert_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    # circuit_breaker_open | cost_anomaly | error_rate | audit_integrity_failure
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    channel_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    muted_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now, nullable=False
    )

    __table_args__ = (
        Index("ix_alert_rules_org_type", "org_id", "type", "enabled"),
    )


class AlertFiring(Base):
    """One row per alert instance. Created on fire, updated on resolve/ack."""
    __tablename__ = "alert_firings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    rule_id: Mapped[str] = mapped_column(String(36), ForeignKey("alert_rules.id"), nullable=False)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="firing")
    # firing | resolved | acked
    fired_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acked_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    context: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    notifications_sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_alert_firings_rule_state", "rule_id", "state"),
        Index("ix_alert_firings_org_state", "org_id", "state", "fired_at"),
    )


# ---------------------------------------------------------------------------
# Raw event log (all event types, for dashboard / metrics pipeline)
# ---------------------------------------------------------------------------


class CloudEventLog(Base):
    """Raw append-only log of every CloudEvent received from the SDK."""
    __tablename__ = "cloud_event_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    event_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(255), nullable=False)
    project: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    __table_args__ = (
        Index("ix_cloud_event_log_org_run", "org_id", "run_id"),
        Index("ix_cloud_event_log_org_type", "org_id", "event_type"),
    )
