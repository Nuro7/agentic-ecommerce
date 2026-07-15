"""Enable order capture: nullable session_id + idempotency key on platform_order_id.

Sales/orders capture persists orders arriving via the Shopify `orders/*` webhooks.
Those orders have no chat session, so `orders.session_id` must allow NULL (it was
NOT NULL, sized only for widget-initiated orders). A unique
(tenant_id, platform_order_id) constraint makes the webhook upsert idempotent:
Shopify redelivers events, and the create/paid/updated topics all reference the
same order id, so we upsert one row instead of duplicating.

platform_order_id stays nullable; Postgres treats NULLs as distinct, so future
session-initiated orders (NULL platform_order_id) never collide on the constraint.

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-15
"""
from __future__ import annotations

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Webhook-captured orders have no chat session.
    op.execute("ALTER TABLE orders ALTER COLUMN session_id DROP NOT NULL;")
    # Idempotent upsert target for the orders/* webhook handlers.
    op.create_unique_constraint(
        "uq_orders_tenant_platform_order",
        "orders",
        ["tenant_id", "platform_order_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_orders_tenant_platform_order", "orders", type_="unique")
    # Backfill NULL session_ids before restoring NOT NULL so downgrade can't fail.
    op.execute("UPDATE orders SET session_id = '' WHERE session_id IS NULL;")
    op.execute("ALTER TABLE orders ALTER COLUMN session_id SET NOT NULL;")
