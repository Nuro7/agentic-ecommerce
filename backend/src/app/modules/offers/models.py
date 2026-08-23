import uuid
from datetime import datetime
from typing import Any, Optional
from sqlalchemy import String, Boolean, DateTime, Text, Float, ForeignKey, func, Integer, Numeric, JSON
from sqlalchemy.orm import Mapped, mapped_column
from ...core.database import Base


class ProductOffer(Base):
    """Merchant-defined promotion rule.

    ``offer_kind`` decides how the deterministic rule engine evaluates it:

    * ``discount``  — single product, percent or fixed amount off.
    * ``dead_stock`` — clearance discount (same math as ``discount``) but flagged
      so the agent pushes it hard and redemption/inventory caps apply.
    * ``combo``     — trigger item(s) in cart unlock reward item(s) at a fixed
      ``combo_price`` (e.g. buy shoe → get 2 watches for a bundle price).
    * ``bulk``      — quantity-based tiers in ``bulk_tiers`` (e.g. qty>=2 → 10%).
    """
    __tablename__ = "product_offers"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), nullable=False, index=True)

    # The primary product being promoted (by platform_id from product_cache).
    # Nullable: combo/bulk offers can span several products, so the primary is
    # informational only (the trigger item for combos).
    platform_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    product_name: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # Marketing label: "promotion", "dead_stock", "new_arrival", "seasonal"
    offer_type: Mapped[str] = mapped_column(String(50), default="promotion")
    # Execution rule kind: "discount" | "dead_stock" | "combo" | "bulk"
    offer_kind: Mapped[str] = mapped_column(String(50), default="discount")

    # Offer details
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    discount_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    discount_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Combo: trigger items unlock reward items at a fixed bundle price.
    # combo_items = [{"platform_id": "123", "quantity": 1, "name": "Shoe"}, ...]
    combo_items: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    combo_price: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)

    # Bulk: tiered quantity discount, ordered ascending by min_qty.
    # bulk_tiers = [{"min_qty": 2, "discount_percent": 10.0}, {"min_qty": 3, "discount_percent": 20.0}]
    bulk_tiers: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # Dead-stock / redemption controls
    max_redemptions: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    redemption_count: Mapped[int] = mapped_column(Integer, default=0)
    inventory_threshold: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Optional pre-created Shopify discount code bound at checkout
    discount_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Active period
    starts_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Priority — higher = shown first by the agent
    priority: Mapped[int] = mapped_column(default=0)

    # Merchant-controlled boost for recommendation ranking (default 1.0)
    merchant_boost: Mapped[float] = mapped_column(Float, default=1.0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
