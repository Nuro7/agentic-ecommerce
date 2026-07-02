"""Add per-tenant store config columns (currency + policies + about).

Without these, currency and shipping/returns/payment policy text come from
deployment-wide env vars (STORE_CURRENCY, STORE_SHIPPING_POLICY, ...) — every
non-Shopify tenant shares identical business info, breaking multi-tenancy for
a SaaS hosting many store types (spices, clothing, kitchen, pharmacy, ...).

All columns nullable: resolution order is tenant column → platform API (where
one exists, e.g. Shopify policies) → env-var fallback, so existing tenants
keep their current behavior until they set values.

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-02
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("currency_symbol", sa.String(10), nullable=True))
    op.add_column("tenants", sa.Column("shipping_policy", sa.Text(), nullable=True))
    op.add_column("tenants", sa.Column("returns_policy", sa.Text(), nullable=True))
    op.add_column("tenants", sa.Column("payment_methods", sa.Text(), nullable=True))
    op.add_column("tenants", sa.Column("about_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("tenants", "about_text")
    op.drop_column("tenants", "payment_methods")
    op.drop_column("tenants", "returns_policy")
    op.drop_column("tenants", "shipping_policy")
    op.drop_column("tenants", "currency_symbol")
