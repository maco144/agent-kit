"""Fleet dashboard metrics tables: active_run_cache, agent_metric_snapshots, circuit_breaker_events.

Revision ID: 002
Revises: 001
Create Date: 2026-03-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "active_run_cache",
        sa.Column("run_id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False),
        sa.Column("project", sa.String(255), nullable=False),
        sa.Column("agent_name", sa.String(255), nullable=False),
        sa.Column("model", sa.String(128), nullable=False, server_default=""),
        sa.Column("started_at", sa.DateTime, nullable=False),
        sa.Column("prompt_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("turns_so_far", sa.Integer, nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_so_far_usd", sa.Float, nullable=False, server_default="0"),
    )
    op.create_index("ix_active_run_cache_org", "active_run_cache", ["org_id"])

    op.create_table(
        "agent_metric_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False),
        sa.Column("project", sa.String(255), nullable=False),
        sa.Column("agent_name", sa.String(255), nullable=False),
        sa.Column("model", sa.String(128), nullable=False, server_default=""),
        sa.Column("bucket", sa.DateTime, nullable=False),
        sa.Column("runs_total", sa.Integer, nullable=False, server_default="0"),
        sa.Column("runs_success", sa.Integer, nullable=False, server_default="0"),
        sa.Column("runs_error", sa.Integer, nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float, nullable=False, server_default="0"),
        sa.Column("total_turns", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_duration_ms", sa.Integer, nullable=False, server_default="0"),
        sa.UniqueConstraint(
            "org_id", "project", "agent_name", "model", "bucket",
            name="uq_metric_snapshot_key",
        ),
    )
    op.create_index("ix_metric_snapshots_org_bucket", "agent_metric_snapshots", ["org_id", "bucket"])
    op.create_index(
        "ix_metric_snapshots_org_agent_bucket",
        "agent_metric_snapshots",
        ["org_id", "project", "agent_name", "bucket"],
    )

    op.create_table(
        "circuit_breaker_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False),
        sa.Column("project", sa.String(255), nullable=False),
        sa.Column("agent_name", sa.String(255), nullable=False),
        sa.Column("resource", sa.String(128), nullable=False),
        sa.Column("prev_state", sa.String(16), nullable=False),
        sa.Column("new_state", sa.String(16), nullable=False),
        sa.Column("failure_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("occurred_at", sa.DateTime, nullable=False),
    )
    op.create_index(
        "ix_cb_events_org_agent_time", "circuit_breaker_events",
        ["org_id", "agent_name", "occurred_at"],
    )
    op.create_index(
        "ix_cb_events_org_state", "circuit_breaker_events",
        ["org_id", "new_state", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_table("circuit_breaker_events")
    op.drop_table("agent_metric_snapshots")
    op.drop_table("active_run_cache")
