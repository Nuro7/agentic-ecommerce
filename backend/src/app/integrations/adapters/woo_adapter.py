"""WooAdapter — converts WooCommerce REST API dicts to CanonicalProduct.

Input: the dict produced by WooCommerceClient._normalize_product_rows() or
       WooCommerceClient._normalize_product_detail()
  {
    "id": int,
    "name": str,
    "price": str,
    "sale_price": str,
    "stock_status": "instock"|"outofstock"|"onbackorder",
    "stock_quantity": int|None,
    "image_url": str,
    "permalink": str,
    "short_description": str,
    "attributes": [{"name": str, "option": str}, ...],   # search results
    "variations_summary": [{
        "id": int,
        "price": str,
        "stock_status": str,
        "stock_quantity": int|None,
        "attributes": [{"name": str, "option": str}],
    }, ...],
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


def _is_in_stock(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    status = str(record.get("stock_status") or "").lower()
    if status:
        return status in ("instock", "onbackorder")
    if isinstance(record.get("in_stock"), bool):
        return bool(record.get("in_stock"))
    qty = record.get("stock_quantity")
    if isinstance(qty, (int, float)):
        return int(qty) > 0
    return True


class WooAdapter:
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
        sale_price_str = raw.get("sale_price") or ""
        sale_price: Optional[float] = _safe_float(sale_price_str) if sale_price_str else None
        on_sale = sale_price is not None and sale_price > 0 and sale_price < regular_price

        in_stock = _is_in_stock(raw)

        # Attributes: WC returns [{"name": "Size", "option": "M"}] (one per used option)
        # or [{"name": "Size", "options": ["S","M","L"]}] from product-level attributes
        attrs: Dict[str, List[str]] = {}
        for attr in raw.get("attributes", []):
            if not isinstance(attr, dict):
                continue
            name = str(attr.get("name", "")).strip()
            if not name:
                continue
            options = attr.get("options")
            option = attr.get("option")
            if options and isinstance(options, list):
                attrs.setdefault(name, []).extend(str(o) for o in options)
            elif option:
                attrs.setdefault(name, []).append(str(option))

        # Variants
        variants: List[CanonicalVariant] = []
        for v in raw.get("variations_summary", []) or raw.get("variations", []):
            if not isinstance(v, dict):
                continue
            v_attrs: Dict[str, str] = {}
            for a in v.get("attributes", []):
                if isinstance(a, dict):
                    name = str(a.get("name", "")).strip()
                    if name:
                        v_attrs[name] = str(a.get("option", ""))
                elif isinstance(v.get("attributes"), dict):
                    v_attrs = {
                        str(k).strip(): str(val)
                        for k, val in v["attributes"].items()
                        if str(k).strip()
                    }
                    break
            v_price = _safe_float(v.get("price") or v.get("sale_price") or price)
            v_in_stock = _is_in_stock(v)
            v_img = v.get("image_url") or v.get("image") or None
            if isinstance(v_img, dict):
                v_img = v_img.get("src") or v_img.get("url") or None
            variants.append(CanonicalVariant(
                id=str(v.get("id") or v.get("variation_id") or ""),
                attributes=v_attrs,
                price=v_price,
                regular_price=_safe_float(v.get("regular_price") or v_price),
                sale_price=_safe_float(v.get("sale_price")) if v.get("sale_price") else None,
                in_stock=v_in_stock,
                stock_quantity=v.get("stock_quantity"),
                image_url=v_img,
            ))

        # Categories
        categories: List[str] = []
        for cat in raw.get("categories", []):
            if isinstance(cat, dict):
                slug = cat.get("slug") or cat.get("name", "")
                if slug:
                    categories.append(str(slug))
            elif isinstance(cat, str):
                categories.append(cat)
        category_slug = categories[0] if categories else None

        # Tags: WC supports explicit tags + flatten attribute options
        tag_list: List[str] = []
        for t in raw.get("tags", []):
            if isinstance(t, dict):
                tag_list.append(str(t.get("name") or t.get("slug") or ""))
            elif isinstance(t, str):
                tag_list.append(t)
        for opts in attrs.values():
            tag_list.extend(opts)
        tags = ", ".join(filter(None, tag_list)) or None

        image_url = raw.get("image_url") or raw.get("image") or None
        if isinstance(image_url, dict):
            image_url = image_url.get("src") or image_url.get("url") or None
        extra_images = [
            img.get("src") or img.get("url") or ""
            for img in raw.get("images", [])
            if isinstance(img, dict)
        ]
        extra_images = [u for u in extra_images if u]

        return CanonicalProduct(
            platform_id=str(raw.get("id", "")),
            platform="woocommerce",
            tenant_id=tenant_id,
            name=str(raw.get("name", "")),
            description=str(raw.get("description") or raw.get("short_description") or ""),
            short_description=str(raw.get("short_description") or ""),
            permalink=str(raw.get("permalink") or ""),
            image_url=image_url,
            extra_images=extra_images,
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
