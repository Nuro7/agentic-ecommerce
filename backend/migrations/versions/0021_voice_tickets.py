"""Voice ticketing: voice_tickets table + per-tenant helpdesk webhook URL + RLS.

The voice assistant auto-creates a support ticket (request_human_support /
create_support_ticket) whenever the customer needs human help — refunds,
damaged orders, or unresolvable catalog queries. This migration persists those
tickets in a merchant-scoped table and enables Postgres RLS so tenant isolation
holds at the DB layer just like the other customer-data tables.

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-03
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "voice_tickets",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False, index=True),
        sa.Column("shop_domain", sa.String(255), nullable=True),
        sa.Column("session_id", sa.String(255), nullable=False, index=True),
        sa.Column("customer_name", sa.String(255), nullable=True),
        sa.Column("customer_phone", sa.String(50), nullable=True),
        sa.Column("customer_email", sa.String(255), nullable=True),
        sa.Column("issue_summary", sa.Text(), nullable=False),
        sa.Column("transcript_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("priority", sa.String(20), nullable=False, server_default="medium"),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("webhook_sent", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_voice_tickets_tenant_status", "voice_tickets", ["tenant_id", "status"])

    # Merchant-configured external helpdesk webhook (Gorgias/Zendesk/custom).
    op.add_column(
        "tenants",
        sa.Column("tickets_webhook_url", sa.String(2048), nullable=True),
    )

    # Row-Level Security: direct tenant_id column — same policy as the other
    # customer-data tables (product_cache, cart_items, conversations, orders).
    op.execute("ALTER TABLE voice_tickets ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE voice_tickets FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON voice_tickets
          USING      (tenant_id = current_setting('app.tenant_id', true))
          WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON voice_tickets;")
    op.execute("ALTER TABLE voice_tickets NO FORCE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE voice_tickets DISABLE ROW LEVEL SECURITY;")
    op.drop_column("tenants", "tickets_webhook_url")
    op.drop_index("ix_voice_tickets_tenant_status")
    op.drop_table("voice_tickets")
