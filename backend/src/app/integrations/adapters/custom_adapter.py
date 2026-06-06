"""CustomAdapter — JSONPath-based dynamic field mapping for arbitrary store APIs.

Merchants with custom storefronts (neither Shopify nor WooCommerce) can provide
a field-mapping config that maps their API response keys to CanonicalProduct fields.

Mapping format:
    {
        "platform_id":    "id",           # direct key name
        "name":           "title",
        "price":          "price.amount", # dot-notation for nested keys
        "in_stock":       "inventory.available",
        "image_url":      "images[0].src",  # basic index access supported
        "category_slug":  "category",
    }

Only CanonicalProduct fields are supported as mapping targets.
Any field not mapped falls back to its Pydantic default.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from .canonical import CanonicalProduct


# ── JSONPath-lite resolver ────────────────────────────────────────────────────

_INDEX_RE = re.compile(r"\[(\d+)\]")


def _resolve(data: Any, path: str) -> Any:
    """Resolve a dot-notation + index path against a nested dict.

    Examples:
        _resolve(data, "price")              → data["price"]
        _resolve(data, "price.amount")       → data["price"]["amount"]
        _resolve(data, "images[0].src")      → data["images"][0]["src"]
    """
    for part in path.split("."):
        if data is None:
            return None
        # Handle array index: e.g. "images[0]"
        m = _INDEX_RE.search(part)
        if m:
            key = part[:m.start()]
            idx = int(m.group(1))
            if key:
                data = data.get(key) if isinstance(data, dict) else None
            if isinstance(data, (list, tuple)) and idx < len(data):
                data = data[idx]
            else:
                return None
        elif isinstance(data, dict):
            data = data.get(part)
        else:
            return None
    return data


def _safe_float(value: Any) -> float:
    try:
        return float(str(value or "0").replace(",", "").strip())
    except Exception:
        return 0.0


def _safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes", "instock")
    return bool(value)


# ── Adapter ───────────────────────────────────────────────────────────────────

class CustomAdapter:
    """JSONPath-based adapter for arbitrary store API responses."""

    # Default mapping assumes WooCommerce-like keys so the adapter works
    # out of the box without configuration for common custom APIs.
    DEFAULT_MAPPING: Dict[str, str] = {
        "platform_id":   "id",
        "name":          "name",
        "price":         "price",
        "regular_price": "regular_price",
        "sale_price":    "sale_price",
        "in_stock":      "in_stock",
        "stock_quantity": "stock_quantity",
        "image_url":     "image_url",
        "permalink":     "permalink",
        "description":   "description",
        "category_slug": "category_slug",
        "tags":          "tags",
    }

    @classmethod
    def normalize(
        cls,
        raw: Dict[str, Any],
        *,
        mapping: Optional[Dict[str, str]] = None,
        tenant_id: str = "",
        platform: str = "custom",
        currency: Optional[str] = None,
    ) -> CanonicalProduct:
        m = {**cls.DEFAULT_MAPPING, **(mapping or {})}

        def get(field: str) -> Any:
            path = m.get(field)
            return _resolve(raw, path) if path else None

        price = _safe_float(get("price"))
        regular_price = _safe_float(get("regular_price") or price)
        sale_price_raw = get("sale_price")
        sale_price: Optional[float] = _safe_float(sale_price_raw) if sale_price_raw else None
        on_sale = sale_price is not None and sale_price > 0 and sale_price < regular_price

        # in_stock can be bool or string
        in_stock_raw = get("in_stock")
        if in_stock_raw is None:
            # fall back to stock_quantity > 0
            qty = get("stock_quantity")
            in_stock = int(qty) > 0 if isinstance(qty, (int, float)) else True
        else:
            in_stock = _safe_bool(in_stock_raw)

        # Tags: may be a list, comma string, or single string
        tags_raw = get("tags")
        if isinstance(tags_raw, list):
            tags: Optional[str] = ", ".join(str(t) for t in tags_raw if t)
        elif isinstance(tags_raw, str):
            tags = tags_raw or None
        else:
            tags = None

        return CanonicalProduct(
            platform_id=str(get("platform_id") or raw.get("id", "")),
            platform=platform,
            tenant_id=tenant_id,
            name=str(get("name") or ""),
            description=str(get("description") or ""),
            short_description=str(get("short_description") or ""),
            permalink=str(get("permalink") or ""),
            image_url=get("image_url") or None,
            price=price,
            regular_price=regular_price,
            sale_price=sale_price if on_sale else None,
            currency=currency or os.getenv("STORE_CURRENCY", "USD"),
            on_sale=on_sale,
            in_stock=in_stock,
            stock_quantity=get("stock_quantity"),
            category_slug=get("category_slug") or None,
            tags=tags,
            raw=raw,
        )

    @classmethod
    def normalize_many(
        cls,
        raws: List[Dict[str, Any]],
        *,
        mapping: Optional[Dict[str, str]] = None,
        tenant_id: str = "",
        platform: str = "custom",
        currency: Optional[str] = None,
    ) -> List[CanonicalProduct]:
        return [
            cls.normalize(r, mapping=mapping, tenant_id=tenant_id,
                          platform=platform, currency=currency)
            for r in raws
        ]
