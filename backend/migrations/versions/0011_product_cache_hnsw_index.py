"""Swap product_cache vector index from IVFFlat to HNSW.

IVFFlat (lists=100) needs tuning per dataset size and degrades on recall as the
catalog grows across tenants. HNSW gives better recall/latency at 100k–1M+ rows
with no list tuning. Queries still pre-filter by tenant_id (btree index from
0007/0010), so the planner narrows to one tenant before/around the ANN scan.

NOTE: on a large existing product_cache, build the HNSW index with
`CREATE INDEX CONCURRENTLY` manually (outside this migration's transaction) to
avoid a write lock. On an empty/small table the in-transaction build below is fine.

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-08
"""
from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_product_cache_embedding")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_product_cache_embedding_hnsw "
        "ON product_cache USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 200)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_product_cache_embedding_hnsw")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_product_cache_embedding "
        "ON product_cache USING ivfflat (embedding vector_cosine_ops) "
        "WITH (lists = 100)"
    )
