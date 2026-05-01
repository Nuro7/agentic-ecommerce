"""Initial: ensure required Postgres extensions exist.

On Supabase these extensions are typically already enabled. The IF NOT
EXISTS clause makes this a safe no-op when they are. The downgrade does
NOT drop them — Supabase may rely on them for other features.

Revision ID: 0001_initial_extensions
Revises:
Create Date: 2026-05-01 00:00:00

"""
from alembic import op

revision: str = "0001_initial_extensions"
down_revision: str | None = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
    op.execute('CREATE EXTENSION IF NOT EXISTS "citext"')


def downgrade() -> None:
    # Do NOT drop extensions on Supabase — other features may use them
    pass
