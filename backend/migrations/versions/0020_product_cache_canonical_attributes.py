"""Add canonical colours[]/sizes[] to product_cache.

Ingest-time canonical attribute extraction (agent/retrieval/attributes.py) writes
normalised colour/size tokens into these arrays, so retrieval filters on them by
equality (colors @> ARRAY[...], sizes @> ARRAY[...]) instead of free-text regex
over name/tags. The GIN indexes keep the array-containment filters fast. Old rows
have NULL arrays; they simply won't match attribute-filtered queries until the
next sync backfills them.

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-31
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "product_cache",
        sa.Column("colors", postgresql.ARRAY(sa.Text()), nullable=True),
    )
    op.add_column(
        "product_cache",
        sa.Column("sizes", postgresql.ARRAY(sa.Text()), nullable=True),
    )
    op.create_index(
        "ix_product_cache_colors", "product_cache", ["colors"], postgresql_using="gin",
    )
    op.create_index(
        "ix_product_cache_sizes", "product_cache", ["sizes"], postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_product_cache_sizes", table_name="product_cache")
    op.drop_index("ix_product_cache_colors", table_name="product_cache")
    op.drop_column("product_cache", "sizes")
    op.drop_column("product_cache", "colors")
