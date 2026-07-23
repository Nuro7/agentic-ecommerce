"""Create product_offers table for merchant promotions / dead stock.

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-23
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "product_offers",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False, index=True),
        sa.Column("platform_id", sa.String(255), nullable=False),
        sa.Column("product_name", sa.String(500), nullable=False),
        sa.Column("offer_type", sa.String(50), nullable=False, server_default="promotion"),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("discount_percent", sa.Float(), nullable=True),
        sa.Column("discount_amount", sa.Float(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_product_offers_tenant_active",
        "product_offers",
        ["tenant_id", "is_active"],
    )


def downgrade() -> None:
    op.drop_index("ix_product_offers_tenant_active")
    op.drop_table("product_offers")
