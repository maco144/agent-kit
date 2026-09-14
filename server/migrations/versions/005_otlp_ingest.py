"""OTLP ingest: audit chain origin and per-run span activity.

Revision ID: 005
Revises: 004
Create Date: 2026-09-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "audit_runs",
        sa.Column("chain_origin", sa.String(16), nullable=False, server_default="client"),
    )
    op.add_column("active_run_cache", sa.Column("last_event_at", sa.DateTime, nullable=True))
    op.add_column("active_run_cache", sa.Column("failure_message", sa.String(500), nullable=True))


def downgrade() -> None:
    op.drop_column("active_run_cache", "failure_message")
    op.drop_column("active_run_cache", "last_event_at")
    op.drop_column("audit_runs", "chain_origin")
