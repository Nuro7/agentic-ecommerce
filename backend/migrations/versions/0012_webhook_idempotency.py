"""Add webhook_events dedup_key for idempotent ingest.

Shopify/Woo redeliver the same webhook on timeout/retry. Without a dedup key the
ingest inserts a duplicate row each time → the queue reprocesses the same event.
A unique (tenant_id, dedup_key) lets ingest skip exact redeliveries.

dedup_key is nullable so pre-existing rows (NULL) don't collide — Postgres treats
NULLs as distinct in a unique index.

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-08
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("webhook_events", sa.Column("dedup_key", sa.String(64), nullable=True))
    op.create_unique_constraint(
        "uq_webhook_events_tenant_dedup", "webhook_events", ["tenant_id", "dedup_key"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_webhook_events_tenant_dedup", "webhook_events", type_="unique")
    op.drop_column("webhook_events", "dedup_key")
