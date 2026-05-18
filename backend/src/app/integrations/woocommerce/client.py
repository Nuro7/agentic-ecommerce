"""WooCommerce REST API client — implements BaseStoreClient."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from ..base.commerce import BaseStoreClient

logger = logging.getLogger(__name__)


class WooCommerceClient(BaseStoreClient):
    """Store-facing API client used by the shopping agent."""

    def __init__(
        self,
        store_url: Optional[str] = None,
        consumer_key: Optional[str] = None,
        consumer_secret: Optional[str] = None,
        redis_client=None,
    ) -> None:
        self.base_url = (store_url or os.getenv("WOOCOMMERCE_STORE_URL", "")).rstrip("/")
        self.consumer_key = consumer_key if consumer_key is not None else os.getenv("WOOCOMMERCE_CONSUMER_KEY", "")
        self.consumer_secret = consumer_secret if consumer_secret is not None else os.getenv("WOOCOMMERCE_CONSUMER_SECRET", "")
        self.shared_secret = os.getenv("SHARED_SECRET", "")
        self.auth_method = os.getenv("WOOCOMMERCE_AUTH_METHOD", "auto").strip().lower()
        self.enable_wc_v3_fallback = os.getenv("WOOCOMMERCE_ENABLE_WC_V3_FALLBACK", "false").strip().lower() == "true"
        self.currency_symbol = os.getenv("STORE_CURRENCY", "₹")
        self.redis = redis_client

        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(6.5, connect=2.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            verify=False,           # LocalWP uses a self-signed cert not trusted by the container
            follow_redirects=True,  # handle any http→https redirects gracefully
        )
        _url_bytes = (self.base_url or "default").encode("utf-8")
        self._cache_prefix = "woo:" + hashlib.md5(_url_bytes).hexdigest()[:8] + ":"

    async def close(self) -> None:
        await self.client.aclose()

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
        raw_query = (query or "").strip()
        normalized_query = self._normalize_catalog_query(raw_query)
        cache_key = (
            f"catalog:search:{normalized_query}:{category_slug}:"
            f"{min_price}:{max_price}:{in_stock_only}:{on_sale}:{limit}"
        )
        cached = await self._cache_get(cache_key)
        if isinstance(cached, list):
            return cached

        per_page = max(1, min(int(limit or 6), 40))
        rows: List[Dict[str, Any]] = []

        # 1) Prefer plugin endpoint
        try:
            plugin_params: Dict[str, Any] = {
                "q": normalized_query,
                "per_page": per_page,
                "in_stock_only": "true" if in_stock_only else "false",
            }
            if category_slug:
                plugin_params["category"] = category_slug
            if min_price is not None:
                plugin_params["min_price"] = str(min_price)
            if max_price is not None:
                plugin_params["max_price"] = str(max_price)
            rows = await self._request(
                "GET",
                "/wp-json/wooagent/v1/products/search",
                params=plugin_params,
                auth_required=False,
                signed=False,
            )
        except Exception as exc:
            logger.warning("Plugin product search failed (%s) — trying Store API next", exc)

        # 2) Public Store API fallback
        if not rows:
            try:
                store_params: Dict[str, Any] = {"per_page": per_page}
                if normalized_query:
                    store_params["search"] = normalized_query
                if category_slug:
                    store_params["category"] = category_slug
                if min_price is not None:
                    store_params["min_price"] = int(float(min_price) * 100)
                if max_price is not None:
                    store_params["max_price"] = int(float(max_price) * 100)
                if on_sale is True:
                    store_params["on_sale"] = "true"
                rows = await self._request(
                    "GET",
                    "/wp-json/wc/store/v1/products",
                    params=store_params,
                    auth_required=False,
                )
            except Exception as exc:
                logger.warning("Store API product search failed (%s) — trying WC v3 next", exc)

        # 3) Authenticated wc/v3 fallback
        if not rows and self.enable_wc_v3_fallback and self.consumer_key and self.consumer_secret:
            wc_params: Dict[str, Any] = {
                "per_page": per_page,
                "status": "publish",
            }
            if normalized_query:
                wc_params["search"] = normalized_query
            if category_slug:
                wc_params["category"] = category_slug
            if in_stock_only:
                wc_params["stock_status"] = "instock"
            if on_sale is True:
                wc_params["on_sale"] = True
            try:
                rows = await self._request(
                    "GET",
                    "/wp-json/wc/v3/products",
                    params=wc_params,
                    auth_required=True,
                )
            except Exception as exc:
                logger.warning("wc/v3 product fallback unavailable: %s", exc)
                rows = []

        # 4) Intent-phrase fallback
        if not rows and (not normalized_query or self._looks_like_intent_phrase(raw_query)):
            try:
                rows = await self._request(
                    "GET",
                    "/wp-json/wooagent/v1/products/search",
                    params={
                        "q": "",
                        "per_page": per_page,
                        "in_stock_only": "false",
                    },
                    auth_required=False,
                    signed=False,
                )
            except Exception:
                rows = []

        products = self._normalize_product_rows(rows, min_price=min_price, max_price=max_price, in_stock_only=in_stock_only)
        if products:
            await self._cache_set(cache_key, products, ttl=300)
        return products

    async def get_product_details(self, product_id: int) -> Dict[str, Any]:
        cache_key = f"product:{int(product_id)}"
        cached = await self._cache_get(cache_key)
        if isinstance(cached, dict):
            return cached

        data: Dict[str, Any] = {}

        try:
            data = await self._request(
                "GET",
                f"/wp-json/wooagent/v1/products/{int(product_id)}",
                auth_required=False,
                signed=False,
            )
        except Exception:
            pass

        if not data:
            try:
                data = await self._request(
                    "GET",
                    f"/wp-json/wc/store/v1/products/{int(product_id)}",
                    auth_required=False,
                )
            except Exception:
                pass

        if not data and self.enable_wc_v3_fallback and self.consumer_key and self.consumer_secret:
            data = await self._request(
                "GET",
                f"/wp-json/wc/v3/products/{int(product_id)}",
                auth_required=True,
            )

        normalized = self._normalize_product_detail(data)
        await self._cache_set(cache_key, normalized, ttl=120)
        return normalized

    async def check_inventory(
        self,
        *,
        product_id: int,
        variation_id: Optional[int] = None,
        attributes: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        detail = await self.get_product_details(int(product_id))
        variations = detail.get("variations", []) if isinstance(detail, dict) else []

        if variation_id:
            for variation in variations:
                if int(variation.get("id") or 0) == int(variation_id):
                    return {
                        "product_id": int(product_id),
                        "variation_id": int(variation.get("id") or 0),
                        "in_stock": self._is_in_stock(variation),
                        "stock_quantity": variation.get("stock_quantity"),
                        "attributes": self._normalize_attributes(variation.get("attributes", [])),
                    }

        matched_variation = False
        for variation in variations:
            attrs = self._normalize_attributes(variation.get("attributes", []))
            if self._variation_matches(attrs, selected_attributes=attributes):
                matched_variation = True
                return {
                    "product_id": int(product_id),
                    "variation_id": int(variation.get("id") or 0),
                    "in_stock": self._is_in_stock(variation),
                    "stock_quantity": variation.get("stock_quantity"),
                    "attributes": attrs,
                }

        if attributes and variations and not matched_variation:
            return {
                "product_id": int(product_id),
                "variation_id": 0,
                "in_stock": False,
                "stock_quantity": 0,
                "attributes": [],
                "variant_not_found": True,
            }

        return {
            "product_id": int(product_id),
            "variation_id": 0,
            "in_stock": self._is_in_stock(detail),
            "stock_quantity": detail.get("stock_quantity"),
            "attributes": [],
        }

    async def get_cart(self, *, session_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/wp-json/wooagent/v1/cart",
            params={"session_id": session_id},
            signed=True,
            auth_required=False,
        )

    async def get_cart_for_session(self, session_id: str) -> Dict[str, Any]:
        cart = await self.get_cart(session_id=session_id)
        count = int(cart.get("item_count") or cart.get("count") or 0)
        cart["item_count"] = count
        cart["is_empty"] = count == 0
        cart["total"] = str(cart.get("total") or "₹0")
        return cart

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
        payload = {
            "session_id": session_id,
            "product_id": int(product_id),
            "variation_id": int(variation_id or 0),
            "quantity": max(1, int(quantity or 1)),
        }
        if isinstance(variation, dict) and variation:
            payload["variation"] = variation

        try:
            return await self._request(
                "POST",
                "/wp-json/wooagent/v1/cart/add",
                json_body=payload,
                signed=True,
                auth_required=False,
            )
        except Exception:
            if int(payload["variation_id"]) != 0:
                raise

            detail = await self.get_product_details(int(product_id))
            fallback_variation = 0
            fallback_variation_data: Dict[str, Any] = {}
            for variation in detail.get("variations", []):
                if self._is_in_stock(variation) and int(variation.get("id") or 0) > 0:
                    fallback_variation = int(variation.get("id"))
                    fallback_variation_data = self._attributes_to_variation_map(variation.get("attributes", []))
                    break

            if fallback_variation <= 0:
                raise

            payload["variation_id"] = fallback_variation
            if fallback_variation_data:
                payload["variation"] = fallback_variation_data
            return await self._request(
                "POST",
                "/wp-json/wooagent/v1/cart/add",
                json_body=payload,
                signed=True,
                auth_required=False,
            )

    async def remove_from_cart(
        self,
        *,
        session_id: str,
        cart_item_key: Optional[str] = None,
        product_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        key = cart_item_key
        if not key and product_id is not None:
            cart = await self.get_cart(session_id=session_id)
            for item in cart.get("items", []):
                if int(item.get("product_id") or 0) == int(product_id):
                    key = item.get("cart_item_key")
                    break

        if not key:
            raise RuntimeError("No matching cart item found to remove")

        return await self._request(
            "POST",
            "/wp-json/wooagent/v1/cart/remove",
            json_body={"session_id": session_id, "cart_item_key": key},
            signed=True,
            auth_required=False,
        )

    async def get_orders(self, *, customer_email: str, limit: int = 5) -> List[Dict[str, Any]]:
        safe_email = customer_email.strip().lower()
        encoded = quote(safe_email)
        orders = await self._request(
            "GET",
            f"/wp-json/wooagent/v1/orders/{encoded}",
            params={"session_id": f"orders-{hashlib.md5(safe_email.encode()).hexdigest()[:10]}"},
            signed=True,
            auth_required=False,
        )
        if not isinstance(orders, list):
            return []
        return orders[: max(1, min(int(limit or 5), 10))]

    async def apply_coupon(self, *, session_id: str, coupon_code: str) -> Dict[str, Any]:
        code = str(coupon_code or "").strip().upper()
        if not code:
            return {"success": False, "message": "No coupon code provided."}
        try:
            result = await self._plugin_post('/cart/coupon', {
                "session_id": session_id,
                "coupon_code": code,
            })
            if isinstance(result, dict) and result.get("success"):
                return {
                    "success": True,
                    "code": code,
                    "message": result.get("message", f"Coupon {code} applied!"),
                    "discount": result.get("discount", ""),
                }
            return {
                "success": False,
                "code": code,
                "message": result.get("message", "Coupon could not be applied.") if isinstance(result, dict) else "Coupon could not be applied.",
            }
        except Exception as e:
            logger.warning("apply_coupon plugin call failed (%s), storing for checkout: %s", code, e)
            return {
                "success": True,
                "code": code,
                "message": f"Got it! Coupon {code} will be applied at checkout.",
            }

    async def get_categories(self) -> List[Dict[str, Any]]:
        cache_key = "catalog:categories"
        cached = await self._cache_get(cache_key)
        if isinstance(cached, list):
            return cached

        rows: List[Dict[str, Any]] = []
        try:
            plugin_rows = await self._plugin_get('/categories')
            if isinstance(plugin_rows, list) and plugin_rows:
                rows = plugin_rows
        except Exception:
            pass
        if not rows:
            try:
                rows = await self._request(
                    "GET",
                    "/wp-json/wc/v3/products/categories",
                    params={"per_page": 100},
                    auth_required=True,
                )
            except Exception:
                rows = []

        categories = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            categories.append({
                "id": row.get("id"),
                "name": row.get("name"),
                "slug": row.get("slug"),
                "count": row.get("count"),
            })

        await self._cache_set(cache_key, categories, ttl=300)
        return categories

    async def get_store_info(self) -> Dict[str, Any]:
        info: Dict[str, Any] = {
            "store_name": os.getenv("STORE_NAME", ""),
            "store_url": self.base_url,
            "currency": os.getenv("STORE_CURRENCY", "₹"),
            "supports_voice_cart": True,
            "about": os.getenv("STORE_ABOUT", ""),
            "shipping_policy": os.getenv("STORE_SHIPPING_POLICY", ""),
            "returns_policy": os.getenv("STORE_RETURNS_POLICY", ""),
            "payment_methods": os.getenv("STORE_PAYMENT_METHODS", ""),
        }

        async def _fetch_general():
            try:
                settings = await self._request("GET", "/wp-json/wc/v3/settings/general", auth_required=True)
                if isinstance(settings, list):
                    kv = {s.get("id"): s.get("value") for s in settings if isinstance(s, dict)}
                    if kv.get("woocommerce_store_address"):
                        info["store_address"] = ", ".join(filter(None, [
                            kv.get("woocommerce_store_address", ""),
                            kv.get("woocommerce_store_address_2", ""),
                            kv.get("woocommerce_store_city", ""),
                            kv.get("woocommerce_store_postcode", ""),
                        ]))
                    if kv.get("woocommerce_email_from_address"):
                        info["contact_email"] = kv["woocommerce_email_from_address"]
                    if kv.get("woocommerce_currency") and not info.get("currency"):
                        info["currency"] = kv["woocommerce_currency"]
                    if kv.get("blogname"):
                        info["store_name"] = kv["blogname"]
            except Exception as e:
                logger.debug("Could not fetch WC general settings: %s", e)

        async def _fetch_payment_gateways():
            if info.get("payment_methods"):
                return
            try:
                gateways = await self._request("GET", "/wp-json/wc/v3/payment_gateways", auth_required=True)
                if isinstance(gateways, list):
                    enabled = [g.get("title") for g in gateways if g.get("enabled") and g.get("title")]
                    if enabled:
                        info["payment_methods"] = ", ".join(enabled)
            except Exception as e:
                logger.debug("Could not fetch payment gateways: %s", e)

        async def _fetch_shipping():
            if info.get("shipping_policy"):
                return
            try:
                zones = await self._request("GET", "/wp-json/wc/v3/shipping/zones", auth_required=True)
                if isinstance(zones, list) and zones:
                    zone_names = [z.get("name") for z in zones if z.get("name") and z.get("name") != "Locations not covered by your other zones"]
                    if zone_names:
                        info["shipping_zones"] = ", ".join(zone_names)
            except Exception as e:
                logger.debug("Could not fetch shipping zones: %s", e)

        await asyncio.gather(
            _fetch_general(),
            _fetch_payment_gateways(),
            _fetch_shipping(),
            return_exceptions=True,
        )

        return {k: v for k, v in info.items() if v not in ("", None)}

    async def find_variants(self, *, product_id: int) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/wp-json/wooagent/v1/products/{int(product_id)}/variations",
            signed=True,
            auth_required=False,
        )

    async def get_product_variations(self, product_id: int) -> dict:
        try:
            product = await self._wc_get(f'/products/{product_id}')
            variations = await self._wc_get(
                f'/products/{product_id}/variations',
                params={'per_page': 100}
            )

            sizes: list = []
            colors: list = []
            formatted_variants = []

            for v in variations:
                attrs = {
                    a['name'].lower(): a['option']
                    for a in v.get('attributes', [])
                }

                size = attrs.get('size') or attrs.get('pa_size', '')
                color = attrs.get('color') or attrs.get('colour') or attrs.get('pa_color', '')

                if size and size not in sizes:
                    sizes.append(size)
                if color and color not in colors:
                    colors.append(color)

                formatted_variants.append({
                    'id': v['id'],
                    'attributes': attrs,
                    'size': size,
                    'color': color,
                    'price': v.get('price', ''),
                    'stock_status': v.get('stock_status', 'instock'),
                    'stock_qty': v.get('stock_quantity'),
                    'image_url': (v.get('image') or {}).get('src', '')
                })

            return {
                'success': True,
                'product_id': product_id,
                'product_name': product.get('name', ''),
                'product_type': product.get('type', 'simple'),
                'available_sizes': sizes,
                'available_colors': colors,
                'variants': formatted_variants,
                'in_stock_count': sum(
                    1 for v in formatted_variants
                    if v['stock_status'] == 'instock'
                )
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_best_coupon(self, cart_total: float = 0) -> dict:
        try:
            coupons = await self._wc_get('/coupons', params={
                'per_page': 100,
                'status': 'publish'
            })

            if not coupons:
                return {'success': True, 'found': False,
                        'message': 'No discount codes available right now'}

            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)

            valid = []
            for c in coupons:
                if c.get('date_expires'):
                    try:
                        exp = datetime.fromisoformat(c['date_expires'].replace('Z', '+00:00'))
                        if exp < now:
                            continue
                    except Exception:
                        pass

                min_amt = float(c.get('minimum_amount') or 0)
                if cart_total > 0 and cart_total < min_amt:
                    continue

                dtype = c.get('discount_type', '')
                amount = float(c.get('amount') or 0)

                if dtype == 'percent':
                    value = (cart_total * amount / 100) if cart_total else amount
                else:
                    value = amount

                valid.append({
                    'code': c['code'],
                    'type': dtype,
                    'amount': amount,
                    'value': value,
                    'min_amount': min_amt,
                    'description': c.get('description', '')
                })

            if not valid:
                return {'success': True, 'found': False,
                        'message': 'No applicable coupons for your cart'}

            best = sorted(valid, key=lambda x: x['value'], reverse=True)[0]

            return {
                'success': True,
                'found': True,
                'code': best['code'],
                'type': best['type'],
                'amount': best['amount'],
                'display': f"{best['amount']}% off" if best['type'] == 'percent'
                           else f"{self.currency_symbol}{best['amount']:.0f} off",
                'estimated_savings': best['value']
            }

        except Exception as e:
            return {"success": False, "error": str(e)}

    async def submit_review(
        self,
        *,
        product_id: int,
        rating: int,
        review: str = "",
        name: Optional[str] = None,
        email: Optional[str] = None,
    ) -> dict:
        try:
            import time as _time
            reviewer_email = email or f"wa_{product_id}_{int(_time.time())}@review.local"

            result = await self._wc_post('/products/reviews', {
                'product_id': product_id,
                'review': review or f"Rated {rating} out of 5 stars.",
                'reviewer': name or 'Customer',
                'reviewer_email': reviewer_email,
                'rating': max(1, min(5, rating)),
                'status': 'hold',
            })

            return {
                'success': True,
                'review_id': result.get('id'),
                'message': 'Review submitted! It will appear after moderation.'
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_reviews(self, product_id: int) -> dict:
        try:
            try:
                plugin_raw = await self._plugin_get(f'/products/{int(product_id)}/reviews')
                if isinstance(plugin_raw, list) and plugin_raw:
                    raw = plugin_raw
                elif isinstance(plugin_raw, dict) and plugin_raw.get('reviews'):
                    raw = plugin_raw['reviews']
                else:
                    raise ValueError("empty plugin response")
            except Exception:
                raw = await self._wc_get('/products/reviews', {
                    'product_id': product_id,
                    'per_page': 10,
                    'status': 'approved',
                })
            if not isinstance(raw, list):
                return {"reviews": [], "count": 0, "average_rating": 0}
            reviews = []
            for r in raw:
                text = re.sub(r'<[^>]+>', '', str(r.get('review') or '')).strip()
                reviews.append({
                    'reviewer': r.get('reviewer') or 'Customer',
                    'rating': int(r.get('rating') or 0),
                    'review': text,
                    'date': str(r.get('date_created') or '')[:10],
                    'verified': bool(r.get('verified_owner', False)),
                })
            avg = round(sum(r['rating'] for r in reviews) / len(reviews), 1) if reviews else 0
            return {'reviews': reviews, 'count': len(reviews), 'average_rating': avg}
        except Exception as e:
            logger.error('get_reviews failed: %s', e)
            return {'reviews': [], 'count': 0, 'average_rating': 0}

    async def get_store_policies(self) -> dict:
        cache_key = 'wooagent:store_policies'

        if self.redis:
            try:
                cached = await self.redis.get(cache_key)
                if cached:
                    return json.loads(cached)
            except Exception:
                pass

        try:
            settings = await self._wc_get('/settings/general')
            general = {s['id']: s.get('value', '') for s in settings}

            zones = await self._wc_get('/shipping/zones')

            gateways = await self._wc_get('/payment_gateways')
            active_gateways = [g['title'] for g in gateways if g.get('enabled')]

            result = {
                'success': True,
                'store_name': general.get('blogname', ''),
                'currency': general.get('woocommerce_currency', 'INR'),
                'currency_symbol': general.get('woocommerce_currency_pos', '₹'),
                'shipping_zones': [z.get('name', '') for z in zones],
                'payment_methods': active_gateways,
                'policies_text': (
                    f"Accepted payments: {', '.join(active_gateways)}. "
                    f"Shipping available to: {', '.join(z.get('name','') for z in zones[:3])}."
                )
            }

            if self.redis:
                await self.redis.setex(cache_key, 3600, json.dumps(result))

            return result

        except Exception as e:
            return {"success": False, "error": str(e)}

    async def pre_warm(self) -> None:
        """Pre-fetch categories + sample products into cache as a background task."""
        async def _warm():
            try:
                await self.get_categories()
                await self.search_products(query="", in_stock_only=True, limit=20)
                await self.get_store_info()
                logger.debug("WC cache pre-warm complete")
            except Exception as e:
                logger.debug("WC pre-warm failed (non-critical): %s", e)
        asyncio.create_task(_warm())

    async def update_cart_quantity(
        self,
        *,
        session_id: str,
        product_id: int,
        quantity: int,
    ) -> dict:
        try:
            if quantity <= 0:
                return await self.remove_from_cart(session_id=session_id, product_id=product_id)

            await self._plugin_post('/cart/update', {
                'session_id': session_id,
                'product_id': product_id,
                'quantity': quantity,
            })

            cart = await self.get_cart(session_id=session_id)
            return {
                'success': True,
                'new_quantity': quantity,
                'updated_cart': cart,
                'message': f"Quantity updated to {quantity}",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── Internal HTTP request ──────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        auth_required: bool,
        signed: bool = False,
    ) -> Any:
        if not self.base_url:
            raise RuntimeError("WOOCOMMERCE_STORE_URL is not configured")

        url = f"{self.base_url}{path}"
        request_params = dict(params or {})
        headers: Dict[str, str] = {}
        auth = (self.consumer_key, self.consumer_secret) if auth_required else None

        serialized_body = ""
        if json_body is not None:
            serialized_body = json.dumps(json_body, separators=(",", ":"), ensure_ascii=False)
            headers["Content-Type"] = "application/json"

        if signed:
            if not self.shared_secret:
                raise RuntimeError("SHARED_SECRET is required for signed WooAgent requests")
            timestamp = str(int(time.time()))
            signature = hmac.new(
                self.shared_secret.encode("utf-8"),
                f"{timestamp}.{path}.{serialized_body}".encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            headers["X-WooAgent-Timestamp"] = timestamp
            headers["X-WooAgent-Signature"] = signature

        retries = 2
        for attempt in range(retries):
            try:
                call_auth = auth
                call_params = dict(request_params)

                if auth_required and self.auth_method == "query":
                    call_auth = None
                    call_params["consumer_key"] = self.consumer_key
                    call_params["consumer_secret"] = self.consumer_secret

                response = await self.client.request(
                    method,
                    url,
                    params=call_params,
                    content=serialized_body if json_body is not None else None,
                    headers=headers,
                    auth=call_auth,
                )

                if (
                    auth_required
                    and self.auth_method == "auto"
                    and response.status_code == 401
                    and self.consumer_key
                    and self.consumer_secret
                ):
                    fallback_params = dict(request_params)
                    fallback_params["consumer_key"] = self.consumer_key
                    fallback_params["consumer_secret"] = self.consumer_secret
                    response = await self.client.request(
                        method,
                        url,
                        params=fallback_params,
                        content=serialized_body if json_body is not None else None,
                        headers=headers,
                        auth=None,
                    )

                response.raise_for_status()
                parsed = response.json() if response.text else {}

                if isinstance(parsed, dict) and {"success", "data", "error"}.issubset(parsed.keys()):
                    if not parsed.get("success", False):
                        raise RuntimeError(str(parsed.get("error") or "WooAgent endpoint failed"))
                    return parsed.get("data")

                return parsed
            except Exception as exc:
                if attempt == retries - 1:
                    exc_str = str(exc)
                    if "401" in exc_str or "403" in exc_str:
                        logger.warning("Woo auth failed %s %s: %s", method, path, exc_str[:120])
                    else:
                        logger.error("Woo request failed %s %s: %s", method, path, exc)
                    raise
                await asyncio.sleep(0.25 * (2**attempt))

        raise RuntimeError("Woo request failed")

    # ── Normalization helpers ──────────────────────────────────────────────────

    def _normalize_product_rows(
        self,
        rows: Any,
        *,
        min_price: Optional[float],
        max_price: Optional[float],
        in_stock_only: bool,
    ) -> List[Dict[str, Any]]:
        products: List[Dict[str, Any]] = []

        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue

            store_prices = row.get("prices", {}) if isinstance(row.get("prices"), dict) else {}
            minor_unit = store_prices.get("currency_minor_unit")

            price = row.get("price") or row.get("regular_price") or store_prices.get("price") or store_prices.get("regular_price") or ""
            sale_price = row.get("sale_price") or store_prices.get("sale_price") or ""

            normalized_price = self._format_price_value(price, minor_unit)
            normalized_sale = self._format_price_value(sale_price, minor_unit)

            numeric_price = self._to_float(normalized_sale or normalized_price)
            if min_price is not None and numeric_price is not None and numeric_price < float(min_price):
                continue
            if max_price is not None and numeric_price is not None and numeric_price > float(max_price):
                continue

            stock_status = str(row.get("stock_status") or "").lower().strip()
            in_stock = self._is_in_stock(row)
            if not stock_status:
                stock_status = "instock" if in_stock else "outofstock"
            if in_stock_only and not in_stock:
                continue

            image_url = ""
            direct_img = row.get("image_url")
            if isinstance(direct_img, str) and direct_img.startswith("http"):
                image_url = direct_img
            if not image_url:
                images_field = row.get("images")
                if isinstance(images_field, list) and images_field:
                    first = images_field[0]
                    if isinstance(first, dict):
                        image_url = first.get("src") or first.get("thumbnail") or first.get("url") or ""
                    elif isinstance(first, str):
                        image_url = first
            if not image_url:
                img_field = row.get("image")
                if isinstance(img_field, dict):
                    image_url = img_field.get("src") or img_field.get("url") or img_field.get("thumbnail") or ""
                elif isinstance(img_field, str) and img_field.startswith("http"):
                    image_url = img_field

            variations = row.get("variations", [])
            if not isinstance(variations, list):
                variations = []

            products.append({
                "id": row.get("id"),
                "name": row.get("name", ""),
                "price": str(normalized_price or ""),
                "sale_price": str(normalized_sale or ""),
                "stock_status": stock_status or ("instock" if in_stock else "outofstock"),
                "stock_quantity": row.get("stock_quantity"),
                "image_url": image_url,
                "permalink": row.get("permalink", ""),
                "short_description": self._strip_html(row.get("short_description") or row.get("description") or ""),
                "attributes": row.get("attributes", []),
                "variations_summary": variations[:8],
            })

        return products

    def _normalize_product_detail(self, data: Dict[str, Any]) -> Dict[str, Any]:
        prices = data.get("prices", {}) if isinstance(data.get("prices"), dict) else {}
        minor_unit = prices.get("currency_minor_unit")
        price = self._format_price_value(data.get("price") or prices.get("price") or "", minor_unit)

        images: List[str] = []
        for image in data.get("images", []) if isinstance(data.get("images"), list) else []:
            if isinstance(image, str) and image:
                images.append(image)
            elif isinstance(image, dict):
                url = image.get("src") or image.get("url") or image.get("thumbnail") or ""
                if url:
                    images.append(str(url))
        if not images:
            img_field = data.get("image")
            if isinstance(img_field, dict):
                url = img_field.get("src") or img_field.get("url") or ""
                if url:
                    images.append(url)
            elif isinstance(img_field, str) and img_field:
                images.append(img_field)

        variations: List[Dict[str, Any]] = []
        for variation in data.get("variations", []) if isinstance(data.get("variations"), list) else []:
            if not isinstance(variation, dict):
                continue
            v_in_stock = self._is_in_stock(variation)
            variations.append({
                "id": variation.get("id") or variation.get("variation_id"),
                "price": variation.get("price", ""),
                "regular_price": variation.get("regular_price", ""),
                "sale_price": variation.get("sale_price", ""),
                "stock_status": variation.get("stock_status", "") or ("instock" if v_in_stock else "outofstock"),
                "stock_quantity": variation.get("stock_quantity"),
                "attributes": self._normalize_attributes(variation.get("attributes", [])),
            })

        root_in_stock = self._is_in_stock(data)
        first_image = images[0] if images else ""

        return {
            "id": data.get("id"),
            "name": data.get("name", ""),
            "description": data.get("description", ""),
            "price": price,
            "stock_quantity": data.get("stock_quantity"),
            "stock_status": data.get("stock_status", "") or ("instock" if root_in_stock else "outofstock"),
            "images": images,
            "image_url": first_image,
            "permalink": data.get("permalink", ""),
            "attributes": data.get("attributes", []),
            "variations": variations,
            "variations_summary": variations,
            "reviews_summary": data.get("reviews_summary", {}),
            "categories": data.get("categories", []),
            "related_products": data.get("related_products", []),
        }

    async def _cache_get(self, key: str) -> Optional[Any]:
        if not self.redis:
            return None
        try:
            namespaced = self._cache_prefix + key
            raw = await asyncio.wait_for(self.redis.get(namespaced), timeout=1.2)
            return json.loads(raw) if raw else None
        except Exception:
            return None

    async def _cache_set(self, key: str, value: Any, ttl: int) -> None:
        if not self.redis:
            return
        try:
            namespaced = self._cache_prefix + key
            await asyncio.wait_for(self.redis.set(namespaced, json.dumps(value), ex=ttl), timeout=1.2)
        except Exception:
            return

    @staticmethod
    def _normalize_catalog_query(query: str) -> str:
        text = str(query or "").strip().lower()
        if not text:
            return ""
        text = re.sub(
            r"\b(show|list|find|search|get|please|what|which|available|availability|products?|items?|catalog|all|me|the|for|is|are|do|does|can|have|has|there|any|in|stock|check|tell|about|you|looking|see|some|a|an)\b",
            " ",
            text,
        )
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _looks_like_intent_phrase(query: str) -> bool:
        q = str(query or "").strip().lower()
        if not q:
            return True
        return bool(
            re.search(r"\b(show|list|find|search)\b", q)
            and re.search(r"\b(products?|items?|catalog)\b", q)
        )

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        try:
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _strip_html(value: str) -> str:
        return re.sub(r"<[^>]+>", " ", str(value or "")).replace("&nbsp;", " ").strip()

    @staticmethod
    def _format_price_value(value: Any, minor_unit: Any) -> str:
        if value in {None, ""}:
            return ""
        raw = str(value).strip()
        if not raw:
            return ""
        try:
            decimals = int(minor_unit) if minor_unit is not None else None
        except Exception:
            decimals = None
        if decimals is None:
            return raw
        if re.fullmatch(r"\d+", raw):
            amount = int(raw) / (10 ** max(0, decimals))
            return f"{amount:.{max(0, decimals)}f}"
        return raw

    @staticmethod
    def _normalize_attributes(attributes: Any) -> List[Dict[str, Any]]:
        if isinstance(attributes, list):
            out: List[Dict[str, Any]] = []
            for item in attributes:
                if isinstance(item, dict):
                    out.append({
                        "name": item.get("name") or item.get("attribute") or "",
                        "option": item.get("option") or item.get("value") or "",
                    })
            return out
        if isinstance(attributes, dict):
            return [{"name": str(k), "option": str(v)} for k, v in attributes.items()]
        return []

    @staticmethod
    def _variation_matches(attributes: List[Dict[str, Any]], *, selected_attributes: Optional[Dict[str, str]]) -> bool:
        if not selected_attributes:
            return False
        matches = True
        for key, expected_val in selected_attributes.items():
            if not expected_val:
                continue
            norm_key = key.lower().strip()
            norm_val = expected_val.lower().strip()
            has_match_for_key = False
            for attr in attributes:
                name = str(attr.get("name", "")).lower().strip()
                option = str(attr.get("option", "")).lower().strip()
                if norm_key in name or name in norm_key:
                    if norm_val in option or option in norm_val:
                        has_match_for_key = True
                        break
            if not has_match_for_key:
                matches = False
                break
        return matches

    @staticmethod
    def _is_in_stock(record: Any) -> bool:
        if not isinstance(record, dict):
            return False
        stock_status = str(record.get("stock_status") or "").lower().strip()
        if stock_status:
            return stock_status in ("instock", "onbackorder")
        if isinstance(record.get("is_in_stock"), bool):
            return bool(record.get("is_in_stock"))
        if isinstance(record.get("in_stock"), bool):
            return bool(record.get("in_stock"))
        stock_qty = record.get("stock_quantity")
        if isinstance(stock_qty, (int, float)):
            if int(stock_qty) > 0:
                return True
            if int(stock_qty) == 0:
                return False
        purchasable = record.get("purchasable")
        if isinstance(purchasable, bool):
            return purchasable
        return True

    @staticmethod
    def _attributes_to_variation_map(attributes: Any) -> Dict[str, str]:
        normalized = WooCommerceClient._normalize_attributes(attributes)
        output: Dict[str, str] = {}
        for attr in normalized:
            raw_name = str(attr.get("name", "")).strip().lower()
            option = str(attr.get("option", "")).strip()
            if not raw_name or not option:
                continue
            if raw_name.startswith("attribute_"):
                key = raw_name
            else:
                key = "attribute_" + re.sub(r"[^a-z0-9_]+", "_", raw_name).strip("_")
            output[key] = option
        return output

    @staticmethod
    def _safe_float(value: Any) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value or '0').strip()
        s = re.sub(r'[₹$€£,\s]', '', s)
        try:
            return float(s) if s else 0.0
        except (ValueError, TypeError):
            return 0.0

    async def _plugin_get(self, path: str, params: Optional[dict] = None) -> Any:
        return await self._request("GET", f"/wp-json/wooagent/v1{path}", params=params, auth_required=False, signed=True)

    async def _plugin_post(self, path: str, json_body: Optional[dict] = None) -> Any:
        return await self._request("POST", f"/wp-json/wooagent/v1{path}", json_body=json_body, auth_required=False, signed=True)

    async def _wc_get(self, path: str, params: Optional[dict] = None) -> Any:
        return await self._request("GET", f"/wp-json/wc/v3{path}", params=params, auth_required=True)

    async def _wc_post(self, path: str, json_body: Optional[dict] = None) -> Any:
        return await self._request("POST", f"/wp-json/wc/v3{path}", json_body=json_body, auth_required=True)


# Backward compatibility alias
WooCommerceService = WooCommerceClient
