"""Extend product_offers for combo/bulk/dead-stock rule engine.

Adds offer_kind (discount|dead_stock|combo|bulk), combo_items/combo_price
JSON structure, bulk_tiers, redemption + inventory caps, and the optional
Shopify discount code bound at checkout. platform_id/product_name become
nullable (combo/bulk offers span several products).

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-10
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # platform_id / product_name become optional (combo + bulk span products)
    op.alter_column(
        "product_offers",
        "platform_id",
        existing_type=sa.String(255),
        nullable=True,
    )
    op.alter_column(
        "product_offers",
        "product_name",
        existing_type=sa.String(500),
        nullable=True,
    )

    op.add_column(
        "product_offers",
        sa.Column("offer_kind", sa.String(50), nullable=False, server_default="discount"),
    )
    json_type = sa.JSON()
    op.add_column("product_offers", sa.Column("combo_items", json_type, nullable=True))
    op.add_column("product_offers", sa.Column("combo_price", sa.Numeric(10, 2), nullable=True))
    op.add_column("product_offers", sa.Column("bulk_tiers", json_type, nullable=True))
    op.add_column("product_offers", sa.Column("max_redemptions", sa.Integer(), nullable=True))
    op.add_column(
        "product_offers",
        sa.Column("redemption_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("product_offers", sa.Column("inventory_threshold", sa.Integer(), nullable=True))
    op.add_column("product_offers", sa.Column("discount_code", sa.String(100), nullable=True))

    # Make offer_kind nullable only for the one-column update below
    op.alter_column(
        "product_offers",
        "offer_kind",
        existing_type=sa.String(50),
        nullable=False,
        existing_server_default="discount",
    )

    if is_postgres:
        # Existing rows: derive offer_kind from offer_type for backwards compat
        op.execute(
            "UPDATE product_offers SET offer_kind = 'dead_stock' WHERE offer_type = 'dead_stock'"
        )
        # keep server default going forward
        op.alter_column(
            "product_offers",
            "offer_kind",
            existing_type=sa.String(50),
            nullable=False,
            server_default="discount",
        )


def downgrade() -> None:
    op.drop_column("product_offers", "discount_code")
    op.drop_column("product_offers", "inventory_threshold")
    op.drop_column("product_offers", "redemption_count")
    op.drop_column("product_offers", "max_redemptions")
    op.drop_column("product_offers", "bulk_tiers")
    op.drop_column("product_offers", "combo_price")
    op.drop_column("product_offers", "combo_items")
    op.drop_column("product_offers", "offer_kind")
    op.alter_column(
        "product_offers",
        "platform_id",
        existing_type=sa.String(255),
        nullable=False,
    )
    op.alter_column(
        "product_offers",
        "product_name",
        existing_type=sa.String(500),
        nullable=False,
    )
