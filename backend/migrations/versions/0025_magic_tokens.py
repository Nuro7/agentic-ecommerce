"""Magic-login tokens for merchant passwordless / first-login auth.

Merchants installed via Shopify OAuth have no password (tenants.hashed_password
is empty), so email+password login can't work for them. This table backs the
"send me a login link" flow: a one-time, expiring magic link is issued, hashed
with SHA-256 at rest, and exchanged for a JWT (+ optional password set) at
POST /auth/magic-verify.

This is separate from refresh_tokens (which are long-lived session tokens).
A magic token is single-use and short-lived (15 min default).

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-07
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "magic_tokens",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(255), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("magic_tokens")