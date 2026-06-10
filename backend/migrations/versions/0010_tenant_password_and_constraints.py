"""Add tenant password + integrity constraints/indexes.

- tenants.hashed_password — enables real login (argon2 hash); nullable so
  existing tenants keep working until they set a password.
- Unique (tenant_id, session_id) on conversations — prevents the get_or_create
  race that created duplicate rows and then raised MultipleResultsFound.
- Foreign-key indexes that were missing on hot list/filter paths.

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-06
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("hashed_password", sa.String(255), nullable=True))

    # De-duplicate conversations before adding the unique constraint, keeping the
    # earliest row per (tenant_id, session_id) and re-parenting its messages.
    op.execute(sa.text("""
        WITH ranked AS (
            SELECT id, tenant_id, session_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY tenant_id, session_id ORDER BY created_at, id
                   ) AS rn,
                   FIRST_VALUE(id) OVER (
                       PARTITION BY tenant_id, session_id ORDER BY created_at, id
                   ) AS keep_id
            FROM conversations
        )
        UPDATE messages m
        SET conversation_id = r.keep_id
        FROM ranked r
        WHERE m.conversation_id = r.id AND r.rn > 1
    """))
    op.execute(sa.text("""
        DELETE FROM conversations c
        USING (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY tenant_id, session_id ORDER BY created_at, id
                   ) AS rn
            FROM conversations
        ) d
        WHERE c.id = d.id AND d.rn > 1
    """))
    op.create_unique_constraint(
        "uq_conversations_tenant_session", "conversations", ["tenant_id", "session_id"]
    )

    # Missing FK / filter indexes (seq-scans at scale otherwise).
    op.create_index("ix_conversations_tenant_id", "conversations", ["tenant_id"])
    op.create_index("ix_cart_items_tenant_id", "cart_items", ["tenant_id"])
    op.create_index("ix_orders_tenant_id", "orders", ["tenant_id"])
    op.create_index("ix_webhook_events_status", "webhook_events", ["status"])


def downgrade() -> None:
    op.drop_index("ix_webhook_events_status", "webhook_events")
    op.drop_index("ix_orders_tenant_id", "orders")
    op.drop_index("ix_cart_items_tenant_id", "cart_items")
    op.drop_index("ix_conversations_tenant_id", "conversations")
    op.drop_constraint("uq_conversations_tenant_session", "conversations", type_="unique")
    op.drop_column("tenants", "hashed_password")
