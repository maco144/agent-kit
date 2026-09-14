"""Budgets for the cost circuit breaker.

Revision ID: 006
Revises: 005
Create Date: 2026-09-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "budgets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("project", sa.String(255), nullable=False, server_default="*"),
        sa.Column("agent_name", sa.String(255), nullable=False, server_default="*"),
        sa.Column("period", sa.String(16), nullable=False),
        sa.Column("limit_usd", sa.Float, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("tripped_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_budgets_org_enabled", "budgets", ["org_id", "enabled"])


def downgrade() -> None:
    op.drop_index("ix_budgets_org_enabled", table_name="budgets")
    op.drop_table("budgets")
