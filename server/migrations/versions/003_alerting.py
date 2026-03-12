"""Alerting tables: alert_channels, alert_rules, alert_firings.

Revision ID: 003
Revises: 002
Create Date: 2026-03-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alert_channels",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("config", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_alert_channels_org", "alert_channels", ["org_id"])

    op.create_table(
        "alert_rules",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("config", sa.JSON, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default="1"),
        sa.Column("channel_ids", sa.JSON, nullable=False),
        sa.Column("muted_until", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_alert_rules_org_type", "alert_rules", ["org_id", "type", "enabled"])

    op.create_table(
        "alert_firings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("rule_id", sa.String(36), sa.ForeignKey("alert_rules.id"), nullable=False),
        sa.Column("org_id", sa.String(36), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="firing"),
        sa.Column("fired_at", sa.DateTime, nullable=False),
        sa.Column("resolved_at", sa.DateTime, nullable=True),
        sa.Column("acked_at", sa.DateTime, nullable=True),
        sa.Column("acked_by", sa.String(255), nullable=True),
        sa.Column("context", sa.JSON, nullable=False),
        sa.Column("notifications_sent", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index("ix_alert_firings_rule_state", "alert_firings", ["rule_id", "state"])
    op.create_index(
        "ix_alert_firings_org_state", "alert_firings", ["org_id", "state", "fired_at"]
    )


def downgrade() -> None:
    op.drop_table("alert_firings")
    op.drop_table("alert_rules")
    op.drop_table("alert_channels")
