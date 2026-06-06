"""CanonicalProduct — single product shape used everywhere inside Speako.

Every platform adapter (Shopify, WooCommerce, Custom) must produce this model.
The agent, retrieval layer, and product-sync task consume only CanonicalProduct,
never raw platform dicts.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator


class CanonicalVariant(BaseModel):
    """A single purchasable variant (size/color combination)."""
    id: str
    attributes: Dict[str, str] = Field(default_factory=dict)
    price: float = 0.0
    regular_price: float = 0.0
    sale_price: Optional[float] = None
    in_stock: bool = True
    stock_quantity: Optional[int] = None
    image_url: Optional[str] = None

    @property
    def on_sale(self) -> bool:
        return self.sale_price is not None and self.sale_price < self.regular_price


class CanonicalProduct(BaseModel):
    """Unified product representation — platform-agnostic.

    Sources:
      - ShopifyAdapter.normalize(raw_node)
      - WooAdapter.normalize(raw_dict)
      - CustomAdapter.normalize(raw_dict, mapping)
    """
    # ── Identity ──────────────────────────────────────────────────────────────
    platform_id: str
    platform: str                         # "shopify" | "woocommerce" | "custom"
    tenant_id: str = ""                   # filled in by sync task

    # ── Display ───────────────────────────────────────────────────────────────
    name: str
    description: str = ""
    short_description: str = ""
    permalink: str = ""
    image_url: Optional[str] = None
    extra_images: List[str] = Field(default_factory=list)

    # ── Pricing ───────────────────────────────────────────────────────────────
    price: float = 0.0
    regular_price: float = 0.0
    sale_price: Optional[float] = None
    currency: str = "USD"
    on_sale: bool = False

    # ── Inventory ─────────────────────────────────────────────────────────────
    in_stock: bool = True
    stock_quantity: Optional[int] = None

    # ── Taxonomy ──────────────────────────────────────────────────────────────
    category_slug: Optional[str] = None
    categories: List[str] = Field(default_factory=list)
    tags: Optional[str] = None            # comma-separated string for FTS index

    # ── Attributes & variants ─────────────────────────────────────────────────
    attributes: Dict[str, List[str]] = Field(default_factory=dict)
    variants: List[CanonicalVariant] = Field(default_factory=list)

    # ── Raw passthrough ───────────────────────────────────────────────────────
    raw: Dict[str, Any] = Field(default_factory=dict, exclude=True)

    @field_validator("price", "regular_price", mode="before")
    @classmethod
    def _coerce_price(cls, v: Any) -> float:
        try:
            return float(str(v or "0").replace(",", "").strip())
        except Exception:
            return 0.0

    def to_cache_dict(self) -> Dict[str, Any]:
        """Dict ready for insertion into the product_cache table."""
        return {
            "platform_id":     self.platform_id,
            "tenant_id":       self.tenant_id,
            "name":            self.name,
            "description":     self.description or self.short_description,
            "price":           self.price,
            "currency":        self.currency,
            "image_url":       self.image_url,
            "in_stock":        self.in_stock,
            "stock_quantity":  self.stock_quantity,
            "category_slug":   self.category_slug,
            "tags":            self.tags,
            "permalink":       self.permalink,
        }
