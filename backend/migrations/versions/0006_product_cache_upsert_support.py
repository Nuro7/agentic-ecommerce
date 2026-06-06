"""Add upsert support to product_cache: unique constraint + stock/permalink columns.

Required by the product sync task (sync_products Celery task).
  - UNIQUE(tenant_id, platform_id)  → enables INSERT ... ON CONFLICT DO UPDATE
  - stock_quantity INTEGER NULL      → exposed in retrieval results
  - permalink TEXT NULL              → product page URL for deep-linking

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-22
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Remove duplicate rows before adding unique constraint.
    # product_cache is a cache — it is safe to discard duplicates.
    op.execute("""
        DELETE FROM product_cache
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM product_cache
            GROUP BY tenant_id, platform_id
        )
    """)

    # Unique constraint that powers INSERT ... ON CONFLICT (tenant_id, platform_id)
    op.create_unique_constraint(
        "uq_product_cache_tenant_platform",
        "product_cache",
        ["tenant_id", "platform_id"],
    )

    # Stock quantity (nullable — not all platforms expose it)
    op.add_column(
        "product_cache",
        sa.Column("stock_quantity", sa.Integer(), nullable=True),
    )

    # Product page URL (for widget deep-linking)
    op.add_column(
        "product_cache",
        sa.Column("permalink", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("product_cache", "permalink")
    op.drop_column("product_cache", "stock_quantity")
    op.drop_constraint(
        "uq_product_cache_tenant_platform",
        "product_cache",
        type_="unique",
    )
