"""Generic REST store adapter — implements BaseStoreClient for custom store APIs.

Convention-based endpoints (relative to base_url):
  Products:   GET  /products/search, GET /products/{id}, GET /products/{id}/variations
  Inventory:  GET  /products/{id}/inventory
  Categories: GET  /categories
  Cart:       GET  /cart, POST /cart/add, POST /cart/remove, PUT /cart/update
  Coupons:    POST /coupons/apply, GET /coupons/best
  Orders:     GET  /orders
  Reviews:    GET  /products/{id}/reviews, POST /products/{id}/reviews
  Store:      GET  /store/info, GET /store/policies

All endpoints receive Authorization: Bearer {api_key} when an api_key is configured.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional

import httpx

from ..base.commerce import BaseStoreClient
from ..adapters.custom_adapter import CustomAdapter
from ...config import settings
from ...core.http_retry import request_with_retries

logger = logging.getLogger(__name__)


class CustomApiClient(BaseStoreClient):
    """
    Plug-in adapter for any store that exposes a custom REST API.
    Set platform=custom_api in tenant config to use this adapter.
    """

    def __init__(self, base_url: str, api_key: str = "", field_mapping: Optional[Dict[str, str]] = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.field_mapping = field_mapping  # optional JSONPath mapping for CustomAdapter
        self._currency = os.getenv("STORE_CURRENCY", "USD")

        headers: Dict[str, str] = {"Content-Type": "application/json", "Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # Per-call HTTP budget is env-tunable per store (see SCALING.md). Defaults
        # match the prior hardcoded 8s/3s. Set CUSTOM_API_TIMEOUT above your store's
        # measured p95; CUSTOM_API_RETRIES bounds total attempts.
        self._timeout = httpx.Timeout(settings.custom_api_timeout, connect=settings.custom_api_connect_timeout)
        self._retries = max(1, settings.custom_api_retries)

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=self._timeout,
            # SSRF hardening: do not follow redirects, which could bounce a
            # validated public URL to an internal address (metadata / RFC1918).
            follow_redirects=False,
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        # Log per-call latency (Render→store path) so operators can measure the
        # store's real p50/p95 and set CUSTOM_API_TIMEOUT above it. Greppable prefix:
        #   grep "custom-api-latency" | grep -oE '[0-9]+ms' | tr -d 'ms' | sort -n
        start = time.monotonic()
        try:
            clean = {k: v for k, v in (params or {}).items() if v is not None}
            resp = await request_with_retries(
                lambda: self._client.get(path, params=clean),
                attempts=self._retries,
                label="custom-api-get",
            )
            resp.raise_for_status()
            logger.info("custom-api-latency GET %s %d %.0fms",
                        path, resp.status_code, (time.monotonic() - start) * 1000)
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.warning("CustomApiClient GET %s → %s (%.0fms)",
                           path, exc.response.status_code, (time.monotonic() - start) * 1000)
            raise
        except Exception as exc:
            logger.warning("CustomApiClient GET %s failed: %s (%.0fms)",
                           path, exc, (time.monotonic() - start) * 1000)
            raise

    async def _post(self, path: str, body: Dict[str, Any]) -> Any:
        try:
            resp = await self._client.post(path, json=body)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.warning("CustomApiClient POST %s → %s", path, exc.response.status_code)
            raise
        except Exception as exc:
            logger.warning("CustomApiClient POST %s failed: %s", path, exc)
            raise

    async def _put(self, path: str, body: Dict[str, Any]) -> Any:
        try:
            resp = await self._client.put(path, json=body)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.warning("CustomApiClient PUT %s → %s", path, exc.response.status_code)
            raise
        except Exception as exc:
            logger.warning("CustomApiClient PUT %s failed: %s", path, exc)
            raise

    def _normalize_products(self, raw_list: Any) -> List[Dict[str, Any]]:
        if not isinstance(raw_list, list):
            raw_list = []
        out = []
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            canon = CustomAdapter.normalize(
                item,
                mapping=self.field_mapping,
                platform="custom",
                currency=self._currency,
            )
            out.append({
                "id":             canon.platform_id,
                "name":           canon.name,
                "price":          str(canon.price),
                "regular_price":  str(canon.regular_price),
                "sale_price":     str(canon.sale_price) if canon.sale_price else "",
                "on_sale":        canon.on_sale,
                "in_stock":       canon.in_stock,
                "stock_quantity": canon.stock_quantity,
                "image_url":      canon.image_url or "",
                "permalink":      canon.permalink or "",
                "description":    canon.description or "",
                "short_description": canon.short_description or "",
                "category_slug":  canon.category_slug or "",
                "tags":           canon.tags or "",
            })
        return out

    def _normalize_product_detail(self, raw: Any) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            return {}
        base = self._normalize_products([raw])
        detail = base[0] if base else {}
        # Preserve raw variations list if present
        detail["variations"] = raw.get("variations") or []
        detail["attributes"] = raw.get("attributes") or {}
        return detail

    # ── Products ──────────────────────────────────────────────────────────────

    async def search_products(
        self,
        *,
        query: str,
        category_slug: Optional[str] = None,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        in_stock_only: bool = True,
        on_sale: Optional[bool] = None,
        limit: int = 6,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {
            "q": query,
            "limit": max(1, min(int(limit), 40)),
            "in_stock_only": "true" if in_stock_only else "false",
        }
        if category_slug:
            params["category"] = category_slug
        if min_price is not None:
            params["min_price"] = min_price
        if max_price is not None:
            params["max_price"] = max_price
        if on_sale is not None:
            params["on_sale"] = "true" if on_sale else "false"

        try:
            data = await self._get("/products/search", params)
            rows = data if isinstance(data, list) else data.get("products") or data.get("data") or []
            return self._normalize_products(rows)
        except Exception:
            return []

    async def get_product_details(self, product_id: int) -> Dict[str, Any]:
        try:
            data = await self._get(f"/products/{str(product_id)}")
            return self._normalize_product_detail(data)
        except Exception:
            return {}

    async def get_products_page(
        self,
        *,
        page: int = 1,
        per_page: int = 100,
        modified_after: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Paginated product fetch — used by the sync task for 1000+ product stores.

        Returns RAW dicts from the store API so the sync task's adapter
        (CustomAdapter.normalize_many) can normalize them exactly once.
        Do NOT call _normalize_products here — that would cause double
        normalization in the sync pipeline.

        Calls GET /products?page={page}&per_page={per_page}.

        Args:
            page:           1-based page number.
            per_page:       Products per page (max 100 recommended).
            modified_after: ISO-8601 string — only return products modified
                            after this timestamp (optional; pass if your API
                            supports it for efficient diff syncs).

        Returns:
            List of raw product dicts from your store API.
            Returns [] on the last page or on error.
        """
        params: Dict[str, Any] = {
            "page":     page,
            "per_page": max(1, min(int(per_page), 200)),
        }
        if modified_after:
            params["modified_after"] = modified_after

        try:
            data = await self._get("/products", params)
            # Accept: list directly, or {"products": [...]} / {"data": [...]}
            rows = data if isinstance(data, list) else data.get("products") or data.get("data") or []
            return [r for r in rows if isinstance(r, dict)]  # raw — no normalization
        except Exception:
            return []

    async def get_product_variations(self, product_id: int) -> dict:
        try:
            data = await self._get(f"/products/{str(product_id)}/variations")
            variations = data if isinstance(data, list) else data.get("variations") or data.get("data") or []
            return {"product_id": str(product_id), "variations": variations}
        except Exception:
            return {"product_id": str(product_id), "variations": []}

    async def find_variants(self, *, product_id: int) -> Dict[str, Any]:
        result = await self.get_product_variations(product_id)
        raw_variations = result.get("variations") or []

        # Fall back to embedded variations inside product detail
        if not raw_variations:
            try:
                detail = await self.get_product_details(product_id)
                raw_variations = detail.get("variations") or []
            except Exception:
                pass

        options: Dict[str, List[str]] = {}
        for v in raw_variations:
            if not isinstance(v, dict):
                continue
            attrs = v.get("attributes") or {}
            if isinstance(attrs, dict):
                for k, val in attrs.items():
                    options.setdefault(str(k), [])
                    if val and str(val) not in options[str(k)]:
                        options[str(k)].append(str(val))
            elif isinstance(attrs, list):
                for attr in attrs:
                    if isinstance(attr, dict):
                        k = attr.get("name") or attr.get("key") or ""
                        val = attr.get("option") or attr.get("value") or ""
                        if k:
                            options.setdefault(str(k), [])
                            if val and str(val) not in options[str(k)]:
                                options[str(k)].append(str(val))
        return {
            "product_id": product_id,
            "has_variants": bool(raw_variations),
            "options": options,
            "variations": raw_variations,   # tool_dispatch and fast_intent expect "variations"
        }

    async def check_inventory(
        self,
        *,
        product_id: int,
        variation_id: Optional[int] = None,
        attributes: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        try:
            params: Dict[str, Any] = {}
            if variation_id:
                params["variation_id"] = variation_id
            if attributes:
                for k, v in attributes.items():
                    params[f"attr_{k}"] = v
            data = await self._get(f"/products/{str(product_id)}/inventory", params if params else None)
            return {
                "product_id":     str(product_id),
                "variation_id":   variation_id or 0,
                "in_stock":       bool(data.get("in_stock", True)),
                "stock_quantity": data.get("stock_quantity"),
                "attributes":     data.get("attributes") or [],
            }
        except Exception:
            # Fall back to product details for inventory info
            try:
                detail = await self.get_product_details(product_id)
                return {
                    "product_id":     str(product_id),
                    "variation_id":   0,
                    "in_stock":       bool(detail.get("in_stock", True)),
                    "stock_quantity": detail.get("stock_quantity"),
                    "attributes":     [],
                }
            except Exception:
                return {"product_id": str(product_id), "variation_id": 0, "in_stock": True, "stock_quantity": None, "attributes": []}

    async def get_categories(self) -> List[Dict[str, Any]]:
        try:
            data = await self._get("/categories")
            cats = data if isinstance(data, list) else data.get("categories") or data.get("data") or []
            out = []
            for c in cats:
                if not isinstance(c, dict):
                    continue
                out.append({
                    "id":    c.get("id") or c.get("slug") or "",
                    "name":  c.get("name") or c.get("title") or "",
                    "slug":  c.get("slug") or "",
                    "count": c.get("count") or c.get("product_count") or 0,
                })
            return out
        except Exception:
            return []

    # ── Cart ──────────────────────────────────────────────────────────────────

    async def get_cart(self, *, session_id: str) -> Dict[str, Any]:
        try:
            # Cart is non-essential to answering a query and sits on the chat/greet
            # critical path — cap it tighter than product calls so a slow/broken store
            # /cart fails fast to an empty cart instead of blocking ~timeout×retries.
            # asyncio.TimeoutError ⊂ Exception, so the except below catches it.
            data = await asyncio.wait_for(
                self._get("/cart", {"session_id": session_id}),
                timeout=settings.custom_api_cart_timeout,
            )
            return self._normalize_cart(data)
        except Exception:
            return {"items": [], "item_count": 0, "total": "0", "is_empty": True}

    async def get_cart_for_session(self, session_id: str) -> Dict[str, Any]:
        return await self.get_cart(session_id=session_id)

    async def get_live_cart(self, session_id: str) -> Dict[str, Any]:
        return await self.get_cart(session_id=session_id)

    async def add_to_cart(
        self,
        *,
        session_id: str,
        product_id: int,
        variation_id: Optional[int] = 0,
        quantity: int = 1,
        variation: Optional[Dict[str, Any]] = None,
        product_name: Optional[str] = None,
        price: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "session_id":   session_id,
            "product_id":   str(product_id),
            "variation_id": int(variation_id or 0),
            "quantity":     max(1, int(quantity or 1)),
        }
        if variation:
            body["variation"] = variation
        try:
            data = await self._post("/cart/add", body)
            success = bool(data.get("success", True)) if isinstance(data, dict) else True
            cart = data.get("cart") or data
            return {"success": success, **self._normalize_cart(cart)}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    async def remove_from_cart(
        self,
        *,
        session_id: str,
        cart_item_key: Optional[str] = None,
        product_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"session_id": session_id}
        if cart_item_key:
            body["cart_item_key"] = cart_item_key
        if product_id:
            body["product_id"] = str(product_id)
        try:
            data = await self._post("/cart/remove", body)
            success = bool(data.get("success", True)) if isinstance(data, dict) else True
            cart = data.get("cart") or data
            return {"success": success, **self._normalize_cart(cart)}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    async def update_cart_quantity(
        self,
        *,
        session_id: str,
        product_id: int,
        quantity: int,
    ) -> dict:
        if quantity <= 0:
            return await self.remove_from_cart(session_id=session_id, product_id=product_id)
        body = {"session_id": session_id, "product_id": str(product_id), "quantity": int(quantity)}
        try:
            data = await self._put("/cart/update", body)
            success = bool(data.get("success", True)) if isinstance(data, dict) else True
            cart = data.get("cart") or data
            return {"success": success, **self._normalize_cart(cart)}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def _normalize_cart(self, data: Any) -> Dict[str, Any]:
        if not isinstance(data, dict):
            return {"items": [], "item_count": 0, "total": "0", "is_empty": True}
        items = data.get("items") or data.get("line_items") or []
        count = int(data.get("item_count") or data.get("count") or len(items))
        total = str(data.get("total") or data.get("subtotal") or "0")
        return {
            "items":      items,
            "item_count": count,
            "total":      total,
            "subtotal":   str(data.get("subtotal") or total),
            "is_empty":   count == 0,
        }

    # ── Discounts ─────────────────────────────────────────────────────────────

    async def apply_coupon(self, *, session_id: str, coupon_code: str) -> Dict[str, Any]:
        try:
            data = await self._post("/coupons/apply", {"session_id": session_id, "coupon_code": coupon_code})
            return {
                "success":       bool(data.get("success", True)),
                "discount":      data.get("discount") or data.get("discount_amount") or "0",
                "message":       data.get("message") or "Coupon applied",
                "total_after":   str(data.get("total") or data.get("new_total") or "0"),
            }
        except Exception as exc:
            return {"success": False, "error": str(exc), "message": "Coupon could not be applied"}

    async def get_best_coupon(self, cart_total: float = 0) -> dict:
        try:
            data = await self._get("/coupons/best", {"cart_total": cart_total})
            coupons = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
            # A valid coupon must have a non-empty code
            best = next((c for c in coupons if isinstance(c, dict) and c.get("code")), None)
            if not best:
                return {}
            return {
                "code":           best.get("code") or "",
                "discount_type":  best.get("discount_type") or "percent",
                "amount":         str(best.get("amount") or best.get("discount") or "0"),
                "description":    best.get("description") or best.get("name") or "",
                "minimum_amount": str(best.get("minimum_amount") or "0"),
                "expiry_date":    best.get("expiry_date") or best.get("date_expires") or "",
            }
        except Exception:
            return {}

    # ── Orders ────────────────────────────────────────────────────────────────

    async def get_orders(self, *, customer_email: str, limit: int = 5) -> List[Dict[str, Any]]:
        try:
            data = await self._get("/orders", {"customer_email": customer_email, "limit": int(limit)})
            orders = data if isinstance(data, list) else data.get("orders") or data.get("data") or []
            out = []
            for o in orders[:limit]:
                if not isinstance(o, dict):
                    continue
                out.append({
                    "id":           str(o.get("id") or o.get("order_id") or ""),
                    "status":       o.get("status") or "unknown",
                    "total":        str(o.get("total") or "0"),
                    "date_created": o.get("date_created") or o.get("created_at") or "",
                    "line_items":   o.get("line_items") or o.get("items") or [],
                    "currency":     o.get("currency") or self._currency,
                    "tracking":     o.get("tracking") or o.get("tracking_number") or "",
                })
            return out
        except Exception:
            return []

    async def create_order(
        self,
        *,
        customer_name: str,
        customer_email: str,
        customer_phone: str,
        address: str,
        city: str,
        postal_code: str,
        country: str = "IN",
        items: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Place a Cash-on-Delivery order via the store's POST /api/orders.

        The order endpoint lives at the STORE ROOT (…/api/orders), NOT under the
        /api/speako base_url — so build the absolute URL by replacing the base's
        last path segment (…/api/speako → …/api/orders).
        """
        order_url = self.base_url.rsplit("/", 1)[0] + "/orders"
        clean_items: List[Dict[str, Any]] = []
        for it in (items or []):
            if not isinstance(it, dict):
                continue
            pid = str(it.get("productId") or it.get("product_id") or it.get("id") or "").strip()
            qty = it.get("quantity") or it.get("qty") or 1
            try:
                qty = max(1, int(qty))
            except (TypeError, ValueError):
                qty = 1
            if pid:
                clean_items.append({"productId": pid, "quantity": qty})
        if not clean_items:
            return {"success": False, "error": "empty_cart", "message": "Your cart is empty — add an item before checkout."}

        body = {
            "customerName":  customer_name,
            "customerEmail": customer_email,
            "customerPhone": customer_phone,
            "address":       address,
            "city":          city,
            "postalCode":    postal_code,
            "country":       country or "IN",
            "items":         clean_items,
        }
        try:
            resp = await request_with_retries(
                lambda: self._client.post(order_url, json=body),
                attempts=self._retries,
                label="custom-api-create-order",
            )
            resp.raise_for_status()
            data = resp.json()
            order = data.get("order") if isinstance(data, dict) else None
            order = order if isinstance(order, dict) else (data if isinstance(data, dict) else {})
            return {
                "success":  True,
                "order_id": str(order.get("id") or order.get("order_id") or order.get("orderNumber") or ""),
                "total":    str(order.get("total") or order.get("totalAmount") or order.get("totalPrice") or ""),
                "message":  (data.get("message") if isinstance(data, dict) else None) or "Order placed successfully",
            }
        except Exception as exc:
            logger.warning("CustomApiClient create_order failed: %s", exc)
            return {"success": False, "error": str(exc), "message": "Sorry, I couldn't place the order just now."}

    # ── Reviews ───────────────────────────────────────────────────────────────

    async def get_reviews(self, product_id: int) -> dict:
        try:
            data = await self._get(f"/products/{str(product_id)}/reviews")
            reviews = data if isinstance(data, list) else data.get("reviews") or data.get("data") or []
            average_rating = None
            if isinstance(data, dict):
                average_rating = data.get("average_rating") or data.get("average") or data.get("rating")
            return {
                "product_id":    str(product_id),
                "reviews":       reviews,
                "count":         len(reviews),
                "average_rating": average_rating,
            }
        except Exception:
            return {"product_id": str(product_id), "reviews": [], "count": 0}

    async def submit_review(
        self,
        *,
        product_id: int,
        rating: int,
        review: str = "",
        name: Optional[str] = None,
        email: Optional[str] = None,
    ) -> dict:
        body: Dict[str, Any] = {
            "product_id": str(product_id),
            "rating":     max(1, min(5, int(rating))),
            "review":     review,
        }
        if name:
            body["name"] = name
        if email:
            body["email"] = email
        try:
            data = await self._post(f"/products/{str(product_id)}/reviews", body)
            return {"success": True, "message": data.get("message") or "Review submitted", "review": data}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    # ── Store info ────────────────────────────────────────────────────────────

    async def get_store_info(self) -> Dict[str, Any]:
        try:
            data = await self._get("/store/info")
            return {
                "name":        data.get("name") or data.get("store_name") or "",
                "description": data.get("description") or "",
                "currency":    data.get("currency") or self._currency,
                "url":         data.get("url") or data.get("store_url") or self.base_url,
                "email":       data.get("email") or data.get("contact_email") or "",
                "phone":       data.get("phone") or data.get("contact_phone") or "",
                "address":     data.get("address") or {},
            }
        except Exception:
            return {"name": "", "currency": self._currency, "url": self.base_url}

    async def get_store_policies(self) -> dict:
        try:
            data = await self._get("/store/policies")
            return {
                "shipping":      data.get("shipping") or data.get("shipping_policy") or "",
                "returns":       data.get("returns") or data.get("return_policy") or data.get("refund_policy") or "",
                "payment":       data.get("payment") or data.get("payment_methods") or "",
                "privacy":       data.get("privacy") or data.get("privacy_policy") or "",
                "terms":         data.get("terms") or data.get("terms_of_service") or "",
            }
        except Exception:
            return {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def pre_warm(self) -> None:
        try:
            await self.get_store_info()
        except Exception:
            pass

    async def close(self) -> None:
        await self._client.aclose()
