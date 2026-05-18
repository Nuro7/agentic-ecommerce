"""
Redis-cached proxy for WooCommerceClient reads.

Write operations (cart, orders, coupons) bypass the cache entirely.
Cache TTLs are tuned for volatility — inventory expires in 2 min,
categories/store-info in 1 hour.

Falls back to direct WooCommerce calls if Redis is unavailable.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Per-resource cache TTLs (seconds)
_TTL = {
    "search":    300,    # 5 min  — products change occasionally
    "product":   600,    # 10 min — details rarely change mid-session
    "variants":  600,    # 10 min
    "stock":     120,    # 2 min  — inventory sells fast
    "categories": 3600,  # 1 hour — very stable
    "storeinfo": 3600,   # 1 hour
    "reviews":   1800,   # 30 min
}


class CachedWooCommerceClient:
    """
    Thin caching proxy around WooCommerceClient.
    All read methods add a Redis cache layer; writes pass straight through.
    """

    def __init__(self, wc_client, redis_client):
        self.wc = wc_client
        self._r = redis_client

    # ── Internal helpers ───────────────────────────────────────────────────

    def _key(self, *parts: Any) -> str:
        return "wc:" + ":".join(str(p) for p in parts)

    def _hash(self, data: Any) -> str:
        return hashlib.md5(
            json.dumps(data, sort_keys=True).encode()
        ).hexdigest()[:12]

    async def _get(self, key: str) -> Optional[Any]:
        if self._r is None:
            return None
        try:
            raw = await self._r.get(key)
            return json.loads(raw) if raw else None
        except Exception as e:
            logger.debug("WC cache GET miss/error for %s: %s", key, e)
            return None

    async def _set(self, key: str, value: Any, ttl: int) -> None:
        if self._r is None:
            return
        try:
            await self._r.setex(key, ttl, json.dumps(value, ensure_ascii=False))
        except Exception as e:
            logger.debug("WC cache SET failed for %s: %s", key, e)

    # ── Cached reads ───────────────────────────────────────────────────────

    async def search_products(self, query: str = "", **filters) -> list:
        store_id = getattr(self.wc, "_store_id", "store")
        key = self._key("search", store_id, self._hash({"q": query, **filters}))
        cached = await self._get(key)
        if cached is not None and len(cached) > 0:
            return cached
        result = await self.wc.search_products(query=query, **filters)
        if result:
            await self._set(key, result, _TTL["search"])
        return result

    async def get_product_details(self, product_id: int) -> dict:
        store_id = getattr(self.wc, "_store_id", "store")
        key = self._key("product", store_id, product_id)
        if (cached := await self._get(key)) is not None:
            return cached
        result = await self.wc.get_product_details(product_id)
        await self._set(key, result, _TTL["product"])
        return result

    async def get_variations(self, product_id: int) -> list:
        store_id = getattr(self.wc, "_store_id", "store")
        key = self._key("variants", store_id, product_id)
        if (cached := await self._get(key)) is not None:
            return cached
        result = await self.wc.get_variations(product_id)
        await self._set(key, result, _TTL["variants"])
        return result

    async def check_inventory(self, product_id: int, **attrs) -> dict:
        store_id = getattr(self.wc, "_store_id", "store")
        key = self._key("stock", store_id, product_id, self._hash(attrs))
        if (cached := await self._get(key)) is not None:
            return cached
        result = await self.wc.check_inventory(product_id, **attrs)
        await self._set(key, result, _TTL["stock"])
        return result

    async def get_categories(self) -> list:
        store_id = getattr(self.wc, "_store_id", "store")
        key = self._key("categories", store_id)
        if (cached := await self._get(key)) is not None:
            return cached
        result = await self.wc.get_categories()
        await self._set(key, result, _TTL["categories"])
        return result

    async def get_store_info(self) -> dict:
        store_id = getattr(self.wc, "_store_id", "store")
        key = self._key("storeinfo", store_id)
        if (cached := await self._get(key)) is not None:
            return cached
        result = await self.wc.get_store_info()
        await self._set(key, result, _TTL["storeinfo"])
        return result

    async def get_reviews(self, product_id: int) -> list:
        store_id = getattr(self.wc, "_store_id", "store")
        key = self._key("reviews", store_id, product_id)
        if (cached := await self._get(key)) is not None:
            return cached
        result = await self.wc.get_reviews(product_id)
        await self._set(key, result, _TTL["reviews"])
        return result

    # ── Never-cached writes ────────────────────────────────────────────────

    async def add_to_cart(self, *args, **kwargs):
        return await self.wc.add_to_cart(*args, **kwargs)

    async def add_multiple_to_cart(self, *args, **kwargs):
        return await self.wc.add_multiple_to_cart(*args, **kwargs)

    async def remove_from_cart(self, *args, **kwargs):
        return await self.wc.remove_from_cart(*args, **kwargs)

    async def update_cart_quantity(self, *args, **kwargs):
        return await self.wc.update_cart_quantity(*args, **kwargs)

    async def get_cart(self, *args, **kwargs):
        return await self.wc.get_cart(*args, **kwargs)

    async def apply_coupon(self, *args, **kwargs):
        return await self.wc.apply_coupon(*args, **kwargs)

    async def get_best_coupon(self, *args, **kwargs):
        return await self.wc.get_best_coupon(*args, **kwargs)

    async def get_orders(self, *args, **kwargs):
        return await self.wc.get_orders(*args, **kwargs)

    async def create_order(self, *args, **kwargs):
        return await self.wc.create_order(*args, **kwargs)

    async def close(self):
        await self.wc.close()

    # ── Delegate any other attributes to underlying client ────────────────

    def __getattr__(self, name: str):
        return getattr(self.wc, name)

    # ── Pre-warm: background task on widget open ───────────────────────────

    async def pre_warm(self) -> None:
        """
        Silently pre-fetches categories + sample products into Redis.
        Call as a background task from the /greet endpoint so the first
        real product query is a cache hit.
        """
        async def _warm():
            try:
                await self.get_categories()
                await self.search_products("", in_stock_only=True, limit=20)
                await self.get_store_info()
                logger.debug("WC cache pre-warm complete")
            except Exception as e:
                logger.debug("WC pre-warm failed (non-critical): %s", e)
        asyncio.create_task(_warm())
