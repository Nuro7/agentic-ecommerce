"""Add Shopify OAuth fields to tenants table.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-18
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("shopify_domain", sa.String(255), nullable=True, unique=True))
    op.add_column("tenants", sa.Column("shopify_access_token", sa.Text(), nullable=True))
    op.add_column("tenants", sa.Column("shopify_scope", sa.Text(), nullable=True))
    op.add_column("tenants", sa.Column("shopify_installed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("tenants", "shopify_installed_at")
    op.drop_column("tenants", "shopify_scope")
    op.drop_column("tenants", "shopify_access_token")
    op.drop_column("tenants", "shopify_domain")
