import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Boolean, DateTime, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from ...core.database import Base
from ...core.crypto import EncryptedText


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255), unique=True)
    plan: Mapped[str] = mapped_column(String(50), default="free")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Argon2 hash for merchant login; nullable until the merchant sets a password.
    hashed_password: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Platform: "shopify" | "woocommerce" | "custom_api"
    platform: Mapped[str] = mapped_column(String(50), default="shopify")

    # Shopify OAuth fields — secret tokens encrypted at rest (EncryptedText).
    shopify_domain: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True)
    shopify_access_token: Mapped[Optional[str]] = mapped_column(EncryptedText, nullable=True)
    shopify_storefront_token: Mapped[Optional[str]] = mapped_column(EncryptedText, nullable=True)
    shopify_scope: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    shopify_installed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # WooCommerce fields
    woocommerce_store_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    woocommerce_consumer_key: Mapped[Optional[str]] = mapped_column(EncryptedText, nullable=True)
    woocommerce_consumer_secret: Mapped[Optional[str]] = mapped_column(EncryptedText, nullable=True)

    # Custom API fields (any store with a custom REST API)
    custom_api_base_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # NOT encrypted: this doubles as the inbound lookup key for /ingest and
    # /onboard/lookup (get_by_custom_api_key does an equality match). Fernet is
    # non-deterministic, so encrypting it would break that lookup. Encrypt later
    # only alongside a deterministic lookup-hash column.
    custom_api_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
