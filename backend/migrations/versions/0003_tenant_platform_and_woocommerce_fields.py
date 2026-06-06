"""Add platform + WooCommerce credential fields to tenants table.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-19
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # platform column — defaults to "shopify" for existing rows
    op.add_column(
        "tenants",
        sa.Column("platform", sa.String(50), nullable=False, server_default="shopify"),
    )
    # Shopify Storefront API token (separate from the OAuth admin token)
    op.add_column(
        "tenants",
        sa.Column("shopify_storefront_token", sa.Text(), nullable=True),
    )
    # WooCommerce credentials
    op.add_column(
        "tenants",
        sa.Column("woocommerce_store_url", sa.String(500), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column("woocommerce_consumer_key", sa.Text(), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column("woocommerce_consumer_secret", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenants", "woocommerce_consumer_secret")
    op.drop_column("tenants", "woocommerce_consumer_key")
    op.drop_column("tenants", "woocommerce_store_url")
    op.drop_column("tenants", "shopify_storefront_token")
    op.drop_column("tenants", "platform")
