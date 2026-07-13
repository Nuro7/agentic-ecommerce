"""Add per-tenant AI configuration columns.

Merchant-configurable AI behavior: custom greeting, personality preset,
support contact, business hours, and a logo URL for future widget use.
All nullable — a tenant with NULL values behaves exactly as before
(hardcoded greeting map, default Aria persona, no contact line).

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-04
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("support_email", sa.String(255), nullable=True))
    op.add_column("tenants", sa.Column("support_phone", sa.String(50), nullable=True))
    op.add_column("tenants", sa.Column("business_hours", sa.String(255), nullable=True))
    op.add_column("tenants", sa.Column("ai_personality", sa.String(20), nullable=True))
    op.add_column("tenants", sa.Column("greeting_message", sa.Text(), nullable=True))
    op.add_column("tenants", sa.Column("logo_url", sa.String(2048), nullable=True))


def downgrade() -> None:
    op.drop_column("tenants", "logo_url")
    op.drop_column("tenants", "greeting_message")
    op.drop_column("tenants", "ai_personality")
    op.drop_column("tenants", "business_hours")
    op.drop_column("tenants", "support_phone")
    op.drop_column("tenants", "support_email")
