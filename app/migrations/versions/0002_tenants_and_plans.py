"""Tenants and Plans tables.

Creates:
  - 4 PostgreSQL ENUMs: tenant_status, platform_kind, billing_interval,
    payment_gateway
  - set_updated_at() trigger function (reused by every future table)
  - tenants table with all indexes, check constraints, RLS
  - plans table with all indexes, check constraints

Revision ID: 0002_tenants_and_plans
Revises: 0001_initial_extensions
Create Date: 2026-05-01 00:01:00

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0002_tenants_and_plans"
down_revision: str | None = "0001_initial_extensions"
branch_labels = None
depends_on = None


# ── helpers ───────────────────────────────────────────────────────────────────

def _exec(sql: str) -> None:
    op.execute(sa.text(sql))


# ── upgrade ───────────────────────────────────────────────────────────────────

def upgrade() -> None:
    # ── ENUMs ─────────────────────────────────────────────────────────────────

    _exec("""
        CREATE TYPE tenant_status AS ENUM (
            'pending_verification',
            'trial',
            'active',
            'past_due',
            'suspended',
            'cancelled'
        )
    """)

    _exec("""
        CREATE TYPE platform_kind AS ENUM (
            'wordpress',
            'shopify',
            'custom'
        )
    """)

    _exec("""
        CREATE TYPE billing_interval AS ENUM (
            'monthly',
            'yearly',
            'one_time'
        )
    """)

    _exec("""
        CREATE TYPE payment_gateway AS ENUM (
            'razorpay',
            'shopify_billing',
            'stripe',
            'manual'
        )
    """)

    # ── Trigger function (shared by all tables with updated_at) ───────────────

    _exec("""
        CREATE OR REPLACE FUNCTION set_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)

    # ── tenants table ─────────────────────────────────────────────────────────

    op.create_table(
        "tenants",
        sa.Column(
            "id", sa.UUID(), nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("slug", sa.String(63), nullable=False),
        sa.Column("legal_name", sa.String(200)),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column(
            "primary_platform",
            sa.Enum(
                "wordpress", "shopify", "custom",
                name="platform_kind", create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending_verification", "trial", "active",
                "past_due", "suspended", "cancelled",
                name="tenant_status", create_type=False,
            ),
            nullable=False,
            server_default="pending_verification",
        ),
        sa.Column("trial_ends_at", sa.DateTime(timezone=True)),
        sa.Column("suspended_at", sa.DateTime(timezone=True)),
        sa.Column("suspended_reason", sa.Text),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("primary_contact_email", sa.String(255), nullable=False),
        sa.Column("primary_contact_name", sa.String(200)),
        sa.Column("support_email", sa.String(255)),
        sa.Column("default_currency", sa.String(3), nullable=False, server_default="INR"),
        sa.Column("default_locale", sa.String(10), nullable=False, server_default="en"),
        sa.Column("timezone", sa.String(50), nullable=False, server_default="UTC"),
        sa.Column("is_internal", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("risk_score", sa.String(20), nullable=False, server_default="unscored"),
        sa.Column(
            "metadata", JSONB, nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_tenants_slug"),
        sa.CheckConstraint(
            r"slug ~ '^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$'",
            name="ck_tenants_slug_format",
        ),
        sa.CheckConstraint(
            r"default_currency ~ '^[A-Z]{3}$'",
            name="ck_tenants_currency_format",
        ),
        sa.CheckConstraint(
            "risk_score IN ('unscored', 'low', 'medium', 'high', 'banned')",
            name="ck_tenants_risk_score",
        ),
        sa.CheckConstraint(
            "(status = 'suspended' AND suspended_at IS NOT NULL) OR status != 'suspended'",
            name="ck_tenants_suspended_consistency",
        ),
        sa.CheckConstraint(
            "(status = 'cancelled' AND cancelled_at IS NOT NULL) OR status != 'cancelled'",
            name="ck_tenants_cancelled_consistency",
        ),
    )

    op.create_index(
        "idx_tenants_status", "tenants", ["status"],
        postgresql_where=sa.text("status != 'cancelled'"),
    )
    op.create_index("idx_tenants_platform", "tenants", ["primary_platform"])
    op.create_index("idx_tenants_contact_email", "tenants", ["primary_contact_email"])
    op.create_index(
        "idx_tenants_trial_ends", "tenants", ["trial_ends_at"],
        postgresql_where=sa.text("status = 'trial'"),
    )
    op.create_index(
        "idx_tenants_recent", "tenants", [sa.text("created_at DESC")]
    )

    # updated_at trigger
    _exec("""
        CREATE TRIGGER trg_tenants_updated_at
            BEFORE UPDATE ON tenants
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)

    # Row-Level Security
    _exec("ALTER TABLE tenants ENABLE ROW LEVEL SECURITY")
    _exec("""
        CREATE POLICY tenant_self_access ON tenants
            FOR ALL
            USING (
                id::text = current_setting('app.current_tenant_id', true)
            )
            WITH CHECK (
                id::text = current_setting('app.current_tenant_id', true)
            )
    """)
    # Service-role / admin bypass:
    # CREATE ROLE app_admin BYPASSRLS; GRANT app_admin TO <db_user>;
    # Run once per environment outside of migrations.

    # ── plans table ───────────────────────────────────────────────────────────

    op.create_table(
        "plans",
        sa.Column(
            "id", sa.UUID(), nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("code", sa.String(50), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("marketing_tagline", sa.String(200)),
        sa.Column("price_paise", sa.BigInteger, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="INR"),
        sa.Column(
            "interval",
            sa.Enum(
                "monthly", "yearly", "one_time",
                name="billing_interval", create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("trial_days", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "gateway",
            sa.Enum(
                "razorpay", "shopify_billing", "stripe", "manual",
                name="payment_gateway", create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("external_plan_id", sa.String(200)),
        sa.Column("is_custom", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("is_publicly_listed", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("display_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("has_overage_pricing", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column(
            "overage_rates", JSONB, nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "metadata", JSONB, nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_plans_code"),
        sa.CheckConstraint("price_paise >= 0", name="ck_plans_price_non_negative"),
        sa.CheckConstraint(
            r"currency ~ '^[A-Z]{3}$'", name="ck_plans_currency_format"
        ),
        sa.CheckConstraint("trial_days >= 0", name="ck_plans_trial_non_negative"),
        sa.CheckConstraint(
            "display_order >= 0", name="ck_plans_display_order_non_negative"
        ),
        sa.CheckConstraint(
            r"code ~ '^[a-z0-9_]+$'", name="ck_plans_code_format"
        ),
    )

    op.create_index(
        "idx_plans_public_listing", "plans", ["display_order"],
        postgresql_where=sa.text(
            "is_active = true AND is_publicly_listed = true AND is_custom = false"
        ),
    )
    op.create_index("idx_plans_gateway", "plans", ["gateway", "is_active"])
    op.create_index(
        "idx_plans_custom", "plans", ["gateway"],
        postgresql_where=sa.text("is_custom = true"),
    )

    # updated_at trigger
    _exec("""
        CREATE TRIGGER trg_plans_updated_at
            BEFORE UPDATE ON plans
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
    """)


# ── downgrade ─────────────────────────────────────────────────────────────────

def downgrade() -> None:
    # Drop RLS first (before table drop).
    _exec("DROP POLICY IF EXISTS tenant_self_access ON tenants")
    _exec("ALTER TABLE tenants DISABLE ROW LEVEL SECURITY")

    # Drop triggers.
    _exec("DROP TRIGGER IF EXISTS trg_plans_updated_at ON plans")
    _exec("DROP TRIGGER IF EXISTS trg_tenants_updated_at ON tenants")

    # Drop tables (plans has no FK to tenants, order doesn't matter here).
    op.drop_table("plans")
    op.drop_table("tenants")

    # Drop trigger function after all tables referencing it are gone.
    _exec("DROP FUNCTION IF EXISTS set_updated_at()")

    # Drop ENUMs last (tables must be gone first).
    _exec("DROP TYPE IF EXISTS payment_gateway")
    _exec("DROP TYPE IF EXISTS billing_interval")
    _exec("DROP TYPE IF EXISTS platform_kind")
    _exec("DROP TYPE IF EXISTS tenant_status")
