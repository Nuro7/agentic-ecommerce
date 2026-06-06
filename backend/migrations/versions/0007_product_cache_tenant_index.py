"""Add B-tree index on product_cache.tenant_id for tenant-scoped sync queries.

Without this index, queries like:
  SELECT * FROM product_cache WHERE tenant_id = $1
do a full sequential scan.  At pilot scale (100 tenants × 1000 products =
100k rows) that's ~10ms, but the index pays off as the table grows.

The BM25 and vector search queries use GIN / ivfflat indexes to find rows
by text/embedding, then filter by tenant_id — Postgres will use whichever
index is cheaper.  The B-tree index helps the planner when tenant_id is
the only filter (sync diff queries, product count queries).

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-23
"""
from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_product_cache_tenant_id",
        "product_cache",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_product_cache_tenant_id", table_name="product_cache")
