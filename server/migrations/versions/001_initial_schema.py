"""Initial schema: organizations, api_keys, audit_runs, audit_events, cloud_event_log.

Revision ID: 001
Revises:
Create Date: 2026-03-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("key_prefix", sa.String(16), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("last_used_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"])

    op.create_table(
        "audit_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("project", sa.String(255), nullable=False),
        sa.Column("agent_name", sa.String(255), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False, unique=True),
        sa.Column("genesis_root", sa.String(64), nullable=False),
        sa.Column("final_root_hash", sa.String(64), nullable=False),
        sa.Column("event_count", sa.Integer, nullable=False, default=0),
        sa.Column("started_at", sa.DateTime, nullable=True),
        sa.Column("completed_at", sa.DateTime, nullable=True),
        sa.Column("integrity", sa.String(16), nullable=False, default="pending"),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_audit_runs_org_id", "audit_runs", ["org_id"])
    op.create_index("ix_audit_runs_org_run_id", "audit_runs", ["org_id", "run_id"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("audit_runs.run_id"), nullable=False),
        sa.Column("org_id", sa.String(36), nullable=False),
        sa.Column("event_id", sa.String(36), nullable=False, unique=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("prev_root", sa.String(64), nullable=False),
        sa.Column("leaf_hash", sa.String(64), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("timestamp", sa.DateTime, nullable=False),
        sa.Column("verified", sa.Boolean, nullable=False, default=False),
        sa.UniqueConstraint("run_id", "seq", name="uq_audit_events_run_seq"),
    )
    op.create_index("ix_audit_events_run_id", "audit_events", ["run_id"])
    op.create_index("ix_audit_events_org_event_type", "audit_events", ["org_id", "event_type"])
    op.create_index("ix_audit_events_leaf_hash", "audit_events", ["leaf_hash"])

    op.create_table(
        "cloud_event_log",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("event_id", sa.String(36), nullable=False, unique=True),
        sa.Column("org_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("agent_name", sa.String(255), nullable=False),
        sa.Column("project", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("occurred_at", sa.DateTime, nullable=False),
        sa.Column("received_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_cloud_event_log_org_run", "cloud_event_log", ["org_id", "run_id"])
    op.create_index("ix_cloud_event_log_org_type", "cloud_event_log", ["org_id", "event_type"])


def downgrade() -> None:
    op.drop_table("cloud_event_log")
    op.drop_table("audit_events")
    op.drop_table("audit_runs")
    op.drop_table("api_keys")
    op.drop_table("organizations")
