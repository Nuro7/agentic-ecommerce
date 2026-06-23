"""Make product_cache.in_stock nullable (store "stock unknown").

The webhook/ingest upsert (webhooks/service.py) deliberately writes in_stock=NULL when
an incoming product has no recognizable stock field — `_parse_stock` returns None for
"unknown" rather than faking True (which would make the agent claim availability it
can't verify). But in_stock was NOT NULL, so the raw INSERT 500'd for stock-less
products and silently dropped those product updates. Dropping NOT NULL lets the
"unknown" state persist; downstream readers already treat a falsy/None in_stock as
not-in-stock (conservative, never over-claims).

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-22
"""
from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE product_cache ALTER COLUMN in_stock DROP NOT NULL;")


def downgrade() -> None:
    # Backfill any NULLs to true before restoring the constraint so downgrade can't fail.
    op.execute("UPDATE product_cache SET in_stock = true WHERE in_stock IS NULL;")
    op.execute("ALTER TABLE product_cache ALTER COLUMN in_stock SET NOT NULL;")
