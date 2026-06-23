"""Enable Postgres Row-Level Security on the customer-conversation tables.

A DB-level backstop for multi-tenant isolation: even a query that forgets
`WHERE tenant_id = …` returns only the current tenant's rows. Each connection sets
`app.tenant_id` transaction-locally (request path via an after_begin event; workers
via set_tenant_guc per tenant) and the policies filter on it.

Scope (tight): product_cache, cart_items, conversations, orders (direct tenant_id)
and messages (scoped via its parent conversation — it has no tenant_id column).
EXCLUDED on purpose: tenants/refresh_tokens/users (auth lookups happen pre-tenant)
and subscriptions/usage_records/webhook_events/conversation_metrics/plans (worker-
written + JWT-read; workers legitimately scan them cross-tenant).

current_setting('app.tenant_id', true) uses missing_ok=true → an unset/empty GUC
yields NULL → no row matches → default-deny (rows hidden, never an error).

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-19
"""
from __future__ import annotations

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

# Tables with a direct `tenant_id` column.
_DIRECT_TABLES = ("product_cache", "cart_items", "conversations", "orders")


def upgrade() -> None:
    for t in _DIRECT_TABLES:
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY;")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {t}
              USING      (tenant_id = current_setting('app.tenant_id', true))
              WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
        """)

    # messages has no tenant_id — scope through its parent conversation.
    op.execute("ALTER TABLE messages ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE messages FORCE ROW LEVEL SECURITY;")
    op.execute("""
        CREATE POLICY tenant_isolation ON messages
          USING (EXISTS (
              SELECT 1 FROM conversations c
              WHERE c.id = messages.conversation_id
                AND c.tenant_id = current_setting('app.tenant_id', true)))
          WITH CHECK (EXISTS (
              SELECT 1 FROM conversations c
              WHERE c.id = messages.conversation_id
                AND c.tenant_id = current_setting('app.tenant_id', true)));
    """)


def downgrade() -> None:
    # Rollback valve — drops policies and disables RLS (no data loss).
    for t in ("messages", *_DIRECT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {t};")
        op.execute(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY;")
