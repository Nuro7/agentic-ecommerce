"""Seed 3 subscription plans: starter, growth, pro.

Starter  — free, 200 credits/month, no voice
Growth   — $29/mo, 1 000 credits/month, voice enabled
Pro      — $79/mo, 5 000 credits/month, voice + analytics

max_conversations column doubles as the monthly credit budget:
  text chat  = 1 credit per session
  voice call = 3 credits per session

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-25
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

# Fixed UUIDs so the migration is deterministic and idempotent.
_STARTER_ID = "00000000-0000-0000-0000-000000000001"
_GROWTH_ID  = "00000000-0000-0000-0000-000000000002"
_PRO_ID     = "00000000-0000-0000-0000-000000000003"


def upgrade() -> None:
    op.execute(sa.text("""
        INSERT INTO plans (id, name, price_monthly, max_conversations, max_stores, features)
        VALUES
            (:starter_id, 'starter', 0.00,  200,  1, '{"allow_voice": false}'),
            (:growth_id,  'growth',  29.00, 1000,  3, '{"allow_voice": true}'),
            (:pro_id,     'pro',     79.00, 5000, 10, '{"allow_voice": true, "analytics": true}')
        ON CONFLICT (name) DO NOTHING
    """).bindparams(
        starter_id=_STARTER_ID,
        growth_id=_GROWTH_ID,
        pro_id=_PRO_ID,
    ))


def downgrade() -> None:
    op.execute(sa.text(
        "DELETE FROM plans WHERE name IN ('starter', 'growth', 'pro')"
    ))
