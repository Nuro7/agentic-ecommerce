"""Order source attribution + line-item capture.

Unlocks the merchant "products sold via Speako" and "AI-revenue/sales-boost"
dashboard features. Two additions:

1. `orders.source`       — 'agent' when the order was driven by the Speako chat
                          widget (session_id stamped onto the cart at add-time
                          survives checkout as an order note/attribute), else
                          NULL for normal store checkout.
2. `order_items` table   — one row per line of every captured order (product_id,
                          name, sku, qty, unit_price, total) so analytics can
                          answer per-product sales and per-offer performance
                          without backfilling any product table.

`order_items` is RLS-forced on its direct `tenant_id` column, mirroring the
other customer-data tables (0013), so cross-tenant leakage is blocked and the
worker write path just calls set_tenant_guc() before inserting.

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-07
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Attribution marker on orders.
    op.add_column("orders", sa.Column("source", sa.String(20), nullable=True))
    op.create_index("ix_orders_source", "orders", ["source"])

    # One row per line of each captured order.
    op.create_table(
        "order_items",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column(
            "order_id", sa.String(), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("product_id", sa.String(255), nullable=True),
        sa.Column("sku", sa.String(255), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("total", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(10), nullable=True),
    )
    op.create_index("ix_order_items_order_id", "order_items", ["order_id"])
    op.create_index("ix_order_items_product_id", "order_items", ["tenant_id", "product_id"])

    op.execute("ALTER TABLE order_items ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE order_items FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON order_items
          USING      (tenant_id = current_setting('app.tenant_id', true))
          WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON order_items;")
    op.execute("ALTER TABLE order_items NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE order_items DISABLE ROW LEVEL SECURITY;")
    op.drop_index("ix_order_items_product_id", table_name="order_items")
    op.drop_index("ix_order_items_order_id", table_name="order_items")
    op.drop_table("order_items")
    op.drop_index("ix_orders_source", table_name="orders")
    op.drop_column("orders", "source")