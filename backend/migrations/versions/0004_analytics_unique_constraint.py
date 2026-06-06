"""Add unique constraint on conversation_metrics(tenant_id, date) for upsert support.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-21
"""
from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_conversation_metrics_tenant_date",
        "conversation_metrics",
        ["tenant_id", "date"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_conversation_metrics_tenant_date",
        "conversation_metrics",
        type_="unique",
    )
