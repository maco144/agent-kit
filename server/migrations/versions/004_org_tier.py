"""Add tier and plan_metadata to organizations.

Revision ID: 004
Revises: 003
Create Date: 2026-03-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("tier", sa.String(16), nullable=False, server_default="free"),
    )
    op.add_column(
        "organizations",
        sa.Column("plan_metadata", sa.JSON, nullable=False, server_default="{}"),
    )
    op.create_index("ix_organizations_tier", "organizations", ["tier"])


def downgrade() -> None:
    op.drop_index("ix_organizations_tier", table_name="organizations")
    op.drop_column("organizations", "plan_metadata")
    op.drop_column("organizations", "tier")
