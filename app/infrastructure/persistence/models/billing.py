"""SQLAlchemy ORM models for Plan, Subscription, UsageRecord tables.

Plan is the global pricing catalogue (not tenant-scoped).
Subscription and UsageRecord are added in Migration 0005.

Populated in: Module 4 — Billing and usage metering.
"""

import enum
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.persistence.database import Base


# ── Python enums ──────────────────────────────────────────────────────────────

class BillingInterval(str, enum.Enum):
    monthly = "monthly"
    yearly = "yearly"
    one_time = "one_time"


class PaymentGateway(str, enum.Enum):
    razorpay = "razorpay"
    shopify_billing = "shopify_billing"
    stripe = "stripe"
    manual = "manual"


# ── PostgreSQL ENUM descriptors ───────────────────────────────────────────────
# create_type=False: migration 0002 owns DDL.

_billing_interval_pg = sa.Enum(
    "monthly", "yearly", "one_time",
    name="billing_interval",
    create_type=False,
)

_payment_gateway_pg = sa.Enum(
    "razorpay", "shopify_billing", "stripe", "manual",
    name="payment_gateway",
    create_type=False,
)


# ── Plan ORM model ────────────────────────────────────────────────────────────

class Plan(Base):
    """Global pricing catalogue.

    Plans are NOT tenant-scoped — they are the menu every tenant picks from.
    Custom (enterprise) plans exist here too but with is_custom=True so they
    are hidden from the public /plans listing.

    No RLS on this table: it is a public catalogue readable by anyone.
    """

    __tablename__ = "plans"

    __table_args__ = (
        # ── Uniqueness ────────────────────────────────────────────────────
        sa.UniqueConstraint("code", name="uq_plans_code"),

        # ── Value checks ──────────────────────────────────────────────────
        # Price is stored in smallest currency unit (paise). Free plans are 0.
        sa.CheckConstraint("price_paise >= 0", name="ck_plans_price_non_negative"),
        sa.CheckConstraint(
            r"currency ~ '^[A-Z]{3}$'", name="ck_plans_currency_format"
        ),
        sa.CheckConstraint("trial_days >= 0", name="ck_plans_trial_non_negative"),
        sa.CheckConstraint(
            "display_order >= 0", name="ck_plans_display_order_non_negative"
        ),
        # Code: lowercase alphanum + underscores. Easy to use in URLs and
        # external system IDs.
        sa.CheckConstraint(
            r"code ~ '^[a-z0-9_]+$'", name="ck_plans_code_format"
        ),

        # ── Indexes ───────────────────────────────────────────────────────
        # The pricing-page query: active, public, non-custom, ordered.
        sa.Index(
            "idx_plans_public_listing", "display_order",
            postgresql_where=sa.text(
                "is_active = true AND is_publicly_listed = true AND is_custom = false"
            ),
        ),
        # Plans by gateway (e.g. find all Razorpay plans).
        sa.Index("idx_plans_gateway", "gateway", "is_active"),
        # Custom (enterprise) plans per gateway.
        sa.Index(
            "idx_plans_custom", "gateway",
            postgresql_where=sa.text("is_custom = true"),
        ),
    )

    # ── Identity ──────────────────────────────────────────────────────────────

    id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )
    # Stable machine identifier. Immutable once shipped — external systems
    # (Razorpay, Shopify) reference plans by code.
    code: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    # Display name shown on pricing page. Safe to change.
    name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    marketing_tagline: Mapped[str | None] = mapped_column(sa.String(200))

    # ── Pricing ───────────────────────────────────────────────────────────────

    # Stored in smallest currency unit (paise for INR, cents for USD).
    # 10000 = INR 100. Never use FLOAT or NUMERIC for money.
    price_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    # Plans are single-currency. Create separate plan rows per currency.
    currency: Mapped[str] = mapped_column(
        sa.String(3), nullable=False, server_default="INR"
    )
    interval: Mapped[str] = mapped_column(_billing_interval_pg, nullable=False)
    # 0 = no trial. Subscription starts as trialing when trialDays > 0.
    trial_days: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )

    # ── Gateway binding ───────────────────────────────────────────────────────

    # Shopify tenants MUST use shopify_billing (App Store policy).
    gateway: Mapped[str] = mapped_column(_payment_gateway_pg, nullable=False)
    # The plan ID in the external gateway. NULL until synced.
    # Without this, no subscription can be created via the gateway.
    external_plan_id: Mapped[str | None] = mapped_column(sa.String(200))

    # ── Visibility ────────────────────────────────────────────────────────────

    # Negotiated enterprise plans; hidden from /plans/public listing.
    is_custom: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )
    # Inactive plans: grandfathered subscribers keep them but no new subs.
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.true()
    )
    # Controls public pricing page. Can be active but unlisted (partner pricing).
    is_publicly_listed: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.true()
    )
    # Lower number = displayed first on pricing page.
    display_order: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )

    # ── Overage pricing ───────────────────────────────────────────────────────

    has_overage_pricing: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )
    # Per-metric overage rates. Shape:
    #   {"sessions.monthly": {"per_unit_paise": 100}, ...}
    overage_rates: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )

    # ── Metadata ──────────────────────────────────────────────────────────────

    # Named plan_metadata to avoid shadowing DeclarativeBase.metadata.
    plan_metadata: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    )

    # ── Timestamps ────────────────────────────────────────────────────────────

    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    # Kept current by set_updated_at() DB trigger (migration 0002).
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    def __repr__(self) -> str:
        return f"<Plan code={self.code!r} price={self.price_paise} {self.currency}>"
