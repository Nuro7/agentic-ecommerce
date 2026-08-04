"""Merchant-facing ticket_number, triage heat, and merchant notes.

Adds three columns to `voice_tickets` (0022) so merchants can reference tickets
by a short human-friendly number (TK-1001), triage by urgency (hot/warm/cold),
and attach their own notes — matching the Zipchat/HelloRep hybrid hand-off model
where the dashboard is a first-class escalation view, not just a webhook sidecar.

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-04
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("voice_tickets", sa.Column("ticket_number", sa.String(20), nullable=True))
    op.add_column("voice_tickets", sa.Column("heat", sa.String(10), nullable=True))
    op.add_column("voice_tickets", sa.Column("merchant_notes", sa.Text(), nullable=True))
    op.create_index(
        "ix_voice_tickets_ticket_number", "voice_tickets", ["ticket_number"]
    )


def downgrade() -> None:
    op.drop_index("ix_voice_tickets_ticket_number", table_name="voice_tickets")
    op.drop_column("voice_tickets", "merchant_notes")
    op.drop_column("voice_tickets", "heat")
    op.drop_column("voice_tickets", "ticket_number")
