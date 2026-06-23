"""Resolve the correct store client for a given platform and credentials.

Per-tenant client cache
-----------------------
create_store_client_for_tenant() is called on every HTTP request and every
WebSocket connection.  Each call previously created a brand-new httpx.AsyncClient
(new TCP connection pool) — wasteful and a connection-leak risk under load.

The module-level _CLIENT_CACHE stores (client, created_at) keyed by tenant_id.
Entries are reused for up to CLIENT_CACHE_TTL seconds (default 5 min).  When
a tenant's credentials change, call invalidate_tenant_client(tenant_id) so the
next request gets a fresh client with the new credentials.

Thread / async safety: all cache mutations are protected by _CACHE_LOCK so
concurrent requests for the same tenant don't race to create duplicate clients.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from ..modules.tenants.models import Tenant

logger = logging.getLogger(__name__)

# ── Per-tenant client cache ───────────────────────────────────────────────────
CLIENT_CACHE_TTL = 300  # seconds — 5 minutes
# Hard cap on cached clients. TTL alone never evicts a tenant that stops sending
# traffic, so the dict grows unbounded with total tenant count (per-process memory
# leak). Oldest-first eviction keeps it bounded. Per-process is fine — see SCALING.md.
CLIENT_CACHE_MAX = int(os.getenv("CLIENT_CACHE_MAX", "500"))

# { tenant_id: (store_client, created_at_monotonic) }
_CLIENT_CACHE: dict[str, tuple[Any, float]] = {}
_CACHE_LOCK = asyncio.Lock()


async def _evict_oldest_if_full() -> None:
    """Evict oldest-by-creation entries until under CLIENT_CACHE_MAX. Caller holds _CACHE_LOCK."""
    while len(_CLIENT_CACHE) >= CLIENT_CACHE_MAX:
        oldest_id = min(_CLIENT_CACHE, key=lambda k: _CLIENT_CACHE[k][1])
        client, _ = _CLIENT_CACHE.pop(oldest_id)
        try:
            await client.close()
        except Exception:
            pass
        logger.debug("Client cache full — evicted oldest tenant=%s", oldest_id)


async def invalidate_tenant_client(tenant_id: str) -> None:
    """Remove a tenant's cached client — call after credential updates."""
    async with _CACHE_LOCK:
        entry = _CLIENT_CACHE.pop(tenant_id, None)
    if entry:
        client, _ = entry
        try:
            await client.close()
        except Exception:
            pass
        logger.debug("Client cache invalidated for tenant=%s", tenant_id)


# ── One-shot factory (no cache) — used by Celery workers ─────────────────────

def create_store_client(platform: str, credentials: dict) -> Any:
    """Create a store client directly from a credentials dict (Celery / scripts)."""
    if platform == "shopify":
        from .shopify.client import ShopifyClient
        return ShopifyClient(
            store_domain=credentials["store_domain"],
            storefront_token=credentials["storefront_token"],
            admin_token=credentials.get("admin_token", ""),
        )
    elif platform == "woocommerce":
        from .woocommerce.client import WooCommerceClient
        return WooCommerceClient(
            store_url=credentials["store_url"],
            consumer_key=credentials["consumer_key"],
            consumer_secret=credentials["consumer_secret"],
        )
    elif platform == "custom_api":
        from .custom_api.client import CustomApiClient
        return CustomApiClient(
            base_url=credentials["base_url"],
            api_key=credentials.get("api_key", ""),
        )
    else:
        raise ValueError(f"Unknown platform: {platform}")


# ── Per-tenant factory with TTL cache (HTTP / WebSocket path) ─────────────────

async def create_store_client_for_tenant(
    tenant: "Tenant",
    redis_client: Optional[Any] = None,
) -> Any:
    """
    Return a store client for the given tenant, reusing a cached instance
    if one exists and is not expired.

    Each tenant gets their own isolated client (their own httpx connection
    pool, their own credentials).  The cache avoids recreating the pool on
    every request while still picking up credential changes quickly.
    """
    tenant_id: str = tenant.id

    async with _CACHE_LOCK:
        cached = _CLIENT_CACHE.get(tenant_id)
        if cached is not None:
            client, created_at = cached
            if time.monotonic() - created_at < CLIENT_CACHE_TTL:
                return client
            # Expired — close and rebuild below
            _CLIENT_CACHE.pop(tenant_id, None)
            try:
                await client.close()
            except Exception:
                pass

        await _evict_oldest_if_full()
        client = _build_client(tenant, redis_client=redis_client)
        _CLIENT_CACHE[tenant_id] = (client, time.monotonic())
        logger.debug(
            "Client cache MISS — created new %s client for tenant=%s",
            tenant.platform, tenant_id,
        )
        return client


def create_store_client_for_tenant_sync(
    tenant: "Tenant",
    redis_client: Optional[Any] = None,
) -> Any:
    """
    Synchronous variant for callers that cannot await (e.g. dependency
    injection wrappers that run in a sync context).  Does NOT use the cache
    — always creates a fresh client.  Prefer the async version when possible.
    """
    return _build_client(tenant, redis_client=redis_client)


# ── Internal builder — platform dispatch ─────────────────────────────────────

def _build_client(tenant: "Tenant", redis_client: Optional[Any] = None) -> Any:
    """Build a brand-new store client from tenant DB credentials."""
    platform = (tenant.platform or "shopify").lower()

    if platform == "shopify":
        from .shopify.client import ShopifyClient
        return ShopifyClient(
            store_domain=tenant.shopify_domain or "",
            storefront_token=tenant.shopify_storefront_token or "",
            admin_token=tenant.shopify_access_token or "",
            redis_client=redis_client,
        )

    elif platform == "woocommerce":
        from .woocommerce.client import WooCommerceClient
        from .woocommerce.cache import CachedWooCommerceClient
        raw = WooCommerceClient(
            store_url=tenant.woocommerce_store_url or "",
            consumer_key=tenant.woocommerce_consumer_key or "",
            consumer_secret=tenant.woocommerce_consumer_secret or "",
            redis_client=redis_client,
        )
        return CachedWooCommerceClient(wc_client=raw, redis_client=redis_client)

    elif platform == "custom_api":
        from .custom_api.client import CustomApiClient
        return CustomApiClient(
            base_url=tenant.custom_api_base_url or "",   # ← fixed: was woocommerce_store_url
            api_key=tenant.custom_api_key or "",          # ← fixed: was hardcoded ""
        )

    else:
        raise ValueError(f"Unknown platform for tenant {tenant.id}: {platform}")
