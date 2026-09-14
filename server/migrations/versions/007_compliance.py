"""Compliance: signing keys, legal holds, deletion receipts, audit retention override.

Revision ID: 007
Revises: 006
Create Date: 2026-09-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("audit_retention_days", sa.Integer, nullable=True))
    op.create_table(
        "signing_keys",
        sa.Column("kid", sa.String(64), primary_key=True),
        sa.Column("public_key", sa.String(64), nullable=False),
        sa.Column("private_key", sa.String(128), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("active", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("retired_at", sa.DateTime, nullable=True),
    )
    op.create_table(
        "legal_holds",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False),
        sa.Column("project", sa.String(255), nullable=True),
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("released_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_legal_holds_org_released", "legal_holds", ["org_id", "released_at"])
    op.create_table(
        "deletion_receipts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("project", sa.String(255), nullable=False),
        sa.Column("agent_name", sa.String(255), nullable=False),
        sa.Column("final_root_hash", sa.String(64), nullable=False),
        sa.Column("event_count", sa.Integer, nullable=False),
        sa.Column("chain_origin", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime, nullable=True),
        sa.Column("completed_at", sa.DateTime, nullable=True),
        sa.Column("deleted_at", sa.DateTime, nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("kid", sa.String(64), nullable=False),
        sa.Column("signature", sa.String(128), nullable=False),
    )
    op.create_index("ix_deletion_receipts_org_deleted", "deletion_receipts", ["org_id", "deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_deletion_receipts_org_deleted", table_name="deletion_receipts")
    op.drop_table("deletion_receipts")
    op.drop_index("ix_legal_holds_org_released", table_name="legal_holds")
    op.drop_table("legal_holds")
    op.drop_table("signing_keys")
    op.drop_column("organizations", "audit_retention_days")
