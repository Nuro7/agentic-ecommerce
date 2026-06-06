"""ShopifyAdapter — converts Shopify Storefront GraphQL nodes to CanonicalProduct.

Input: the dict produced by ShopifyClient._normalize_product_node()
  {
    "id": int,
    "name": str,
    "price": str,
    "sale_price": str,
    "regular_price": str,
    "stock_status": "instock"|"outofstock",
    "stock_quantity": int|None,
    "image_url": str,
    "permalink": str,
    "short_description": str,
    "attributes": [{"name": str, "options": [str, ...]}, ...],
    "variations_summary": [{
        "id": int,
        "attributes": {str: str},
        "price": str,
        "stock_status": str,
        "stock_qty": int|None,
    }, ...],
    "on_sale": bool,
  }

Output: CanonicalProduct
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from .canonical import CanonicalProduct, CanonicalVariant


def _safe_float(value: Any) -> float:
    try:
        return float(str(value or "0").replace(",", "").strip())
    except Exception:
        return 0.0


class ShopifyAdapter:
    """Stateless adapter — call normalize() as a classmethod."""

    @classmethod
    def normalize(
        cls,
        raw: Dict[str, Any],
        *,
        tenant_id: str = "",
        currency: Optional[str] = None,
    ) -> CanonicalProduct:
        price = _safe_float(raw.get("price") or raw.get("regular_price") or 0)
        regular_price = _safe_float(raw.get("regular_price") or raw.get("price") or 0)
        sale_price_raw = raw.get("sale_price")
        sale_price: Optional[float] = _safe_float(sale_price_raw) if sale_price_raw else None
        on_sale = bool(raw.get("on_sale", False)) or (
            sale_price is not None and sale_price < regular_price and sale_price > 0
        )

        in_stock = raw.get("stock_status", "instock") == "instock"

        # Attributes: Shopify returns [{"name": "Size", "options": ["S", "M", "L"]}]
        attrs: Dict[str, List[str]] = {}
        for attr in raw.get("attributes", []):
            if isinstance(attr, dict):
                name = str(attr.get("name", "")).strip()
                options = attr.get("options") or attr.get("values") or []
                if name and options:
                    attrs[name] = [str(o) for o in options]

        # Variants
        variants: List[CanonicalVariant] = []
        for v in raw.get("variations_summary", []):
            if not isinstance(v, dict):
                continue
            v_attrs = v.get("attributes", {})
            if not isinstance(v_attrs, dict):
                v_attrs = {}
            v_price = _safe_float(v.get("price") or price)
            v_in_stock = v.get("stock_status", "instock") == "instock"
            v_img = v.get("image_url") or v.get("image") or None
            if isinstance(v_img, dict):
                v_img = v_img.get("url") or v_img.get("src") or None
            variants.append(CanonicalVariant(
                id=str(v.get("id", "")),
                attributes={str(k): str(val) for k, val in v_attrs.items()},
                price=v_price,
                regular_price=v_price,
                in_stock=v_in_stock,
                stock_quantity=v.get("stock_qty"),
                image_url=v_img,
            ))

        # Category: Shopify doesn't set this in search results — empty by default
        categories: List[str] = [
            c.get("slug") or c.get("name", "")
            for c in raw.get("categories", [])
            if isinstance(c, dict) and (c.get("slug") or c.get("name"))
        ]
        category_slug = categories[0] if categories else None

        # Tags: join all attribute option values for FTS boost
        tag_parts = [opt for opts in attrs.values() for opt in opts]
        tags = ", ".join(tag_parts) if tag_parts else None

        return CanonicalProduct(
            platform_id=str(raw.get("id", "")),
            platform="shopify",
            tenant_id=tenant_id,
            name=str(raw.get("name", "")),
            description=str(raw.get("short_description") or raw.get("description") or ""),
            short_description=str(raw.get("short_description") or ""),
            permalink=str(raw.get("permalink") or ""),
            image_url=raw.get("image_url") or None,
            extra_images=[],
            price=price,
            regular_price=regular_price,
            sale_price=sale_price if on_sale else None,
            currency=currency or os.getenv("STORE_CURRENCY", "USD"),
            on_sale=on_sale,
            in_stock=in_stock,
            stock_quantity=raw.get("stock_quantity"),
            category_slug=category_slug,
            categories=categories,
            tags=tags,
            attributes=attrs,
            variants=variants,
            raw=raw,
        )

    @classmethod
    def normalize_many(
        cls,
        raws: List[Dict[str, Any]],
        *,
        tenant_id: str = "",
        currency: Optional[str] = None,
    ) -> List[CanonicalProduct]:
        return [cls.normalize(r, tenant_id=tenant_id, currency=currency) for r in raws]
