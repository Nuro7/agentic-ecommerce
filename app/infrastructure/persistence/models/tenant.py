"""SQLAlchemy ORM model for the Tenant table.

Includes all indexes and check constraints from the schema design.
Row-Level Security DDL is applied by migration 0002.

Populated in: Module 2 — Tenant core and data isolation.
"""

import enum
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.persistence.database import Base


# ── Python enums (application code) ───────────────────────────────────────────

class TenantStatus(str, enum.Enum):
    pending_verification = "pending_verification"
    trial = "trial"
    active = "active"
    past_due = "past_due"
    suspended = "suspended"
    cancelled = "cancelled"


class PlatformKind(str, enum.Enum):
    wordpress = "wordpress"
    shopify = "shopify"
    custom = "custom"


# ── PostgreSQL ENUM descriptors ───────────────────────────────────────────────
# create_type=False: migration 0002 owns all DDL for these types.

_tenant_status_pg = sa.Enum(
    "pending_verification", "trial", "active", "past_due", "suspended", "cancelled",
    name="tenant_status",
    create_type=False,
)

_platform_kind_pg = sa.Enum(
    "wordpress", "shopify", "custom",
    name="platform_kind",
    create_type=False,
)


# ── ORM model ─────────────────────────────────────────────────────────────────

class Tenant(Base):
    """Root aggregate. Every tenant-scoped table FKs here directly or indirectly.

    Soft-deleted via status=cancelled — never hard-deleted so billing records
    and audit logs remain valid forever.
    RLS is enabled: the app sets app.current_tenant_id per transaction.
    """

    __tablename__ = "tenants"

    __table_args__ = (
        # ── Uniqueness ────────────────────────────────────────────────────
        sa.UniqueConstraint("slug", name="uq_tenants_slug"),

        # ── Format checks ─────────────────────────────────────────────────
        # DNS-safe slug: lowercase alphanum + hyphens, 3-63 chars.
        sa.CheckConstraint(
            r"slug ~ '^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$'",
            name="ck_tenants_slug_format",
        ),
        # ISO 4217 uppercase 3-letter code.
        sa.CheckConstraint(
            r"default_currency ~ '^[A-Z]{3}$'",
            name="ck_tenants_currency_format",
        ),
        # Known risk score values.
        sa.CheckConstraint(
            "risk_score IN ('unscored', 'low', 'medium', 'high', 'banned')",
            name="ck_tenants_risk_score",
        ),

        # ── State consistency ─────────────────────────────────────────────
        # status=suspended must have a suspension timestamp.
        sa.CheckConstraint(
            "(status = 'suspended' AND suspended_at IS NOT NULL) OR status != 'suspended'",
            name="ck_tenants_suspended_consistency",
        ),
        # status=cancelled must have a cancellation timestamp.
        sa.CheckConstraint(
            "(status = 'cancelled' AND cancelled_at IS NOT NULL) OR status != 'cancelled'",
            name="ck_tenants_cancelled_consistency",
        ),

        # ── Indexes ───────────────────────────────────────────────────────
        # Partial: cancelled tenants are rare in operational queries.
        sa.Index(
            "idx_tenants_status", "status",
            postgresql_where=sa.text("status != 'cancelled'"),
        ),
        sa.Index("idx_tenants_platform", "primary_platform"),
        sa.Index("idx_tenants_contact_email", "primary_contact_email"),
        # Partial: only trial rows need trial-expiry sweeping.
        sa.Index(
            "idx_tenants_trial_ends", "trial_ends_at",
            postgresql_where=sa.text("status = 'trial'"),
        ),
        # DESC so recent-signups queries skip an explicit sort.
        sa.Index("idx_tenants_recent", sa.text("created_at DESC")),
    )

    # ── Primary identity ──────────────────────────────────────────────────────

    id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )
    # URL-safe, immutable once set. Used in admin URLs, webhook signatures,
    # and as a stable Redis key prefix.
    slug: Mapped[str] = mapped_column(sa.String(63), nullable=False)
    # Legal/billing name (on invoices). May differ from display_name.
    legal_name: Mapped[str | None] = mapped_column(sa.String(200))
    # Public-facing name shown in dashboards.
    display_name: Mapped[str] = mapped_column(sa.String(200), nullable=False)

    # ── Platform binding ──────────────────────────────────────────────────────

    # Drives adapter selection, billing gateway, and onboarding flow.
    primary_platform: Mapped[str] = mapped_column(_platform_kind_pg, nullable=False)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    status: Mapped[str] = mapped_column(
        _tenant_status_pg, nullable=False, server_default="pending_verification"
    )
    trial_ends_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    suspended_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    suspended_reason: Mapped[str | None] = mapped_column(sa.Text)
    cancelled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    # ── Contact info ──────────────────────────────────────────────────────────

    # Not unique on tenants: one person can own multiple tenants.
    # Uniqueness lives on tenant_users.email (Migration 0003).
    primary_contact_email: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    primary_contact_name: Mapped[str | None] = mapped_column(sa.String(200))
    support_email: Mapped[str | None] = mapped_column(sa.String(255))

    # ── Locale ────────────────────────────────────────────────────────────────

    default_currency: Mapped[str] = mapped_column(
        sa.String(3), nullable=False, server_default="INR"
    )
    default_locale: Mapped[str] = mapped_column(
        sa.String(10), nullable=False, server_default="en"
    )
    timezone: Mapped[str] = mapped_column(
        sa.String(50), nullable=False, server_default="UTC"
    )

    # ── Internal flags ────────────────────────────────────────────────────────

    # Demo/QA tenants: excluded from analytics; billing in test mode.
    is_internal: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )
    # Updated by background job for fraud detection.
    risk_score: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, server_default="unscored"
    )

    # ── Flexible metadata ─────────────────────────────────────────────────────

    # Named tenant_metadata in Python to avoid shadowing DeclarativeBase.metadata.
    # Maps to the 'metadata' column in Postgres.
    tenant_metadata: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    )

    # ── Timestamps ────────────────────────────────────────────────────────────

    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    # Kept current by the set_updated_at() DB trigger (migration 0002).
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    def __repr__(self) -> str:
        return f"<Tenant id={self.id} slug={self.slug!r} status={self.status}>"
