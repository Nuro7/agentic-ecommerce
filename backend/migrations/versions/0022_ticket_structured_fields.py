"""Structured ticket fields: issue_type, order_id, product_id, priority_reason, source.

Adds structured classification columns to `voice_tickets` (0021) so the merchant
dashboard can group / filter tickets by problem type, order, product and priority
reason instead of parsing free-text `issue_summary`. Also records where the ticket
originated (deterministic escalation vs. LLM tool call).

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-03
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("voice_tickets", sa.Column("issue_type", sa.String(50), nullable=True))
    op.add_column("voice_tickets", sa.Column("order_id", sa.String(100), nullable=True))
    op.add_column("voice_tickets", sa.Column("product_id", sa.String(50), nullable=True))
    op.add_column("voice_tickets", sa.Column("priority_reason", sa.String(255), nullable=True))
    op.add_column(
        "voice_tickets",
        sa.Column("source", sa.String(20), nullable=False, server_default="llm"),
    )
    op.create_index(
        "ix_voice_tickets_tenant_issue", "voice_tickets", ["tenant_id", "issue_type"]
    )


def downgrade() -> None:
    op.drop_index("ix_voice_tickets_tenant_issue", table_name="voice_tickets")
    op.drop_column("voice_tickets", "source")
    op.drop_column("voice_tickets", "priority_reason")
    op.drop_column("voice_tickets", "product_id")
    op.drop_column("voice_tickets", "order_id")
    op.drop_column("voice_tickets", "issue_type")
