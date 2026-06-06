"""Add custom_api_base_url and custom_api_key columns to tenants table.

Allows per-tenant credential storage for custom_api platform — same pattern
already used for shopify_* and woocommerce_* columns.  Without these columns
all custom_api tenants share one global env-var URL, breaking multi-tenancy.

Also adds a B-tree index on tenants.platform so factory lookups by platform
stay fast as the tenants table grows.

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-25
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Per-tenant custom API base URL  (e.g. "https://mystore.com/api")
    op.add_column(
        "tenants",
        sa.Column("custom_api_base_url", sa.String(500), nullable=True),
    )
    # Per-tenant custom API secret key sent as Bearer token
    op.add_column(
        "tenants",
        sa.Column("custom_api_key", sa.Text(), nullable=True),
    )
    # Index on platform for fast factory resolution
    op.create_index("ix_tenants_platform", "tenants", ["platform"])


def downgrade() -> None:
    op.drop_index("ix_tenants_platform", table_name="tenants")
    op.drop_column("tenants", "custom_api_key")
    op.drop_column("tenants", "custom_api_base_url")
