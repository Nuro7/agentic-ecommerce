"""Celery task — sync store products into product_cache table.

Feeds the L3 BM25 + vector retrieval layer with real product data so searches
hit PostgreSQL instead of falling back to the live store API every time.

Multi-tenant design
-------------------
Every active tenant has its own platform + credentials stored in the tenants
table.  The sync task iterates all active tenants and builds a SEPARATE store
client for each one from their own DB credentials — never from global env vars.

Pipeline per tenant per sync run:
  1. Build a per-tenant store client from DB credentials (Shopify / WooCommerce
     / Custom API — whichever that tenant registered with).
  2. Fetch ALL products from that tenant's store:
       Shopify     → Bulk Operations API (unlimited) with paginated fallback
       WooCommerce → full paginated REST crawl (page through all products)
       Custom API  → paginated via get_products_page() or single large request
  3. Normalize via CanonicalProduct adapter matching the tenant's platform.
  4. Batch-embed product text via OpenAI text-embedding-3-small (degrades
     gracefully when OPENAI_API_KEY is absent — embedding column stays NULL).
  5. Upsert into product_cache  (INSERT ... ON CONFLICT DO UPDATE).
     Conflict key: (tenant_id, platform_id).

Schedule (set in schedules.py):
  - Nightly at 02:30 UTC       → full refresh of all tenants
  - Triggered on-demand        → after product webhooks (tenant_id passed in)
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, List, Optional

import httpx
from sqlalchemy import text

from ..celery_app import celery_app

logger = logging.getLogger(__name__)

_EMBED_BATCH = 50
_WOO_PAGE_SIZE = 100
_SHOPIFY_PAGE_SIZE = 40
_BULK_TIMEOUT = 600
_CUSTOM_PAGE_SIZE = 100   # per page for custom API pagination
_SYNC_LOCK_TTL = 1800     # 30 min — longer than any single sync run


def _acquire_sync_lock(key: str, ttl: int = _SYNC_LOCK_TTL):
    """Best-effort cross-worker lock. Returns (client, acquired).

    client is None when Redis is unavailable → proceed WITHOUT a lock (fail-open,
    so a Redis outage doesn't stop syncs). acquired is False only when another
    run already holds the lock → caller should skip the duplicate.
    """
    try:
        import redis as _redis
        from ...config import settings
        client = _redis.from_url(settings.redis_url)
        acquired = bool(client.set(key, "1", nx=True, ex=ttl))
        return client, acquired
    except Exception as exc:
        logger.warning("sync lock unavailable (%s) — proceeding without lock", exc)
        return None, True


def _close_lock(client) -> None:
    """Release the standalone lock Redis client's connection pool."""
    if client is None:
        return
    try:
        client.close()
    except Exception:
        pass


# ── Celery entry points ───────────────────────────────────────────────────────

@celery_app.task(
    name="src.app.workers.tasks.sync_products.sync_products",
    bind=True,
    max_retries=2,
    default_retry_delay=300,
)
def sync_products(self, tenant_id: Optional[str] = None) -> dict:
    """Sync all active tenants' products into product_cache.

    Args:
        tenant_id: Sync only this tenant when provided (webhook trigger).
                   When None, syncs every active tenant.
    """
    # Idempotency: a webhook-triggered sync and the nightly run (or duplicate
    # enqueues) must not crawl + upsert the same tenant concurrently.
    lock_key = f"speako:sync_lock:{tenant_id or 'all'}"
    lock_client, acquired = _acquire_sync_lock(lock_key)
    if not acquired:
        logger.info("Product sync already running for %s — skipping duplicate", tenant_id or "all")
        _close_lock(lock_client)  # don't leak the connection on the skip path
        return {"skipped_duplicate": True, "tenants": 0, "upserted": 0, "skipped": 0}

    try:
        result = asyncio.run(_sync_async(tenant_id_filter=tenant_id))
        logger.info(
            "Product sync complete: tenants=%d upserted=%d skipped=%d",
            result["tenants"], result["upserted"], result["skipped"],
        )
        return result
    except Exception as exc:
        logger.error("Product sync failed: %s", exc, exc_info=True)
        raise self.retry(exc=exc)
    finally:
        if lock_client is not None:
            try:
                lock_client.delete(lock_key)
            except Exception:
                pass
            _close_lock(lock_client)


@celery_app.task(
    name="src.app.workers.tasks.sync_products.sync_products_diff",
    bind=True,
    max_retries=2,
    default_retry_delay=300,
)
def sync_products_diff(self, tenant_id: Optional[str] = None) -> dict:
    """Incremental reconciliation — runs every 4 h between nightly full syncs."""
    try:
        result = asyncio.run(_diff_sync_async(tenant_id_filter=tenant_id))
        logger.info(
            "Diff sync complete: tenants=%d upserted=%d full_syncs=%d",
            result["tenants"], result["upserted"], result["full_syncs"],
        )
        return result
    except Exception as exc:
        logger.error("Diff sync failed: %s", exc, exc_info=True)
        raise self.retry(exc=exc)


# ── Full sync ─────────────────────────────────────────────────────────────────

async def _sync_async(tenant_id_filter: Optional[str] = None) -> dict:
    from ...core.database import worker_session as AsyncSessionLocal, set_tenant_guc
    from ...modules.tenants.repository import TenantRepository

    total_upserted = 0
    total_skipped = 0
    tenants_processed = 0

    redis_cli = _open_async_redis()

    async with AsyncSessionLocal() as db:
        repo = TenantRepository(db)
        tenants = await repo.list_all(limit=500)

        for tenant in tenants:
            if tenant_id_filter and tenant.id != tenant_id_filter:
                continue
            if not tenant.is_active:
                continue

            # Scope RLS to this tenant for the product_cache upserts/deletes below.
            # (The tenants scan above is on an RLS-excluded table, so it ran un-scoped.)
            await set_tenant_guc(db, tenant.id)

            store_client = None
            try:
                # ── Build per-tenant client from DB credentials ────────────
                store_client = _build_client_for_tenant(tenant)
                adapter = _adapter_for_platform(tenant.platform)

                upserted, skipped = await _sync_tenant(
                    db=db,
                    store_client=store_client,
                    adapter=adapter,
                    tenant=tenant,
                )
                total_upserted += upserted
                total_skipped += skipped
                tenants_processed += 1

                await _cleanup_deleted(db, tenant.id)

                # Purge the L1/L2 retrieval cache now that product_cache is fresh,
                # so stale prices/products don't keep being served for 5-15 min.
                await _invalidate_retrieval_cache(redis_cli, tenant.id)

                logger.info(
                    "Tenant %s (%s) synced: upserted=%d skipped=%d",
                    tenant.id, tenant.platform, upserted, skipped,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to sync tenant=%s platform=%s: %s",
                    tenant.id, getattr(tenant, "platform", "?"), exc,
                    exc_info=True,
                )
            finally:
                if store_client is not None:
                    try:
                        await store_client.close()
                    except Exception:
                        pass

        await db.commit()

    await _close_async_redis(redis_cli)

    return {
        "tenants": tenants_processed,
        "upserted": total_upserted,
        "skipped": total_skipped,
    }


# ── Retrieval-cache invalidation helpers ──────────────────────────────────────

def _open_async_redis():
    """Standalone async Redis client for cache invalidation. None on failure
    (fail-open — a Redis outage must not abort the sync)."""
    try:
        import redis.asyncio as _aioredis
        from ...config import settings
        return _aioredis.from_url(settings.redis_url, decode_responses=True)
    except Exception as exc:
        logger.warning("Async Redis unavailable for cache invalidation: %s", exc)
        return None


async def _close_async_redis(client) -> None:
    if client is None:
        return
    try:
        await client.aclose()
    except Exception:
        pass


async def _invalidate_retrieval_cache(client, tenant_id: str) -> None:
    """Purge L1+L2 search cache for a tenant after its product_cache is refreshed."""
    if client is None:
        return
    try:
        from ...agent.retrieval.cache import invalidate_tenant
        await invalidate_tenant(client, tenant_id)
    except Exception as exc:
        logger.warning("Retrieval cache invalidation failed tenant=%s: %s", tenant_id, exc)


# ── Diff sync ─────────────────────────────────────────────────────────────────

_COUNT_DRIFT_THRESHOLD = 0.10


async def _diff_sync_async(tenant_id_filter: Optional[str] = None) -> dict:
    from ...core.database import worker_session as AsyncSessionLocal, set_tenant_guc
    from ...modules.tenants.repository import TenantRepository

    total_upserted = 0
    total_full_syncs = 0
    tenants_processed = 0

    redis_cli = _open_async_redis()

    async with AsyncSessionLocal() as db:
        repo = TenantRepository(db)
        tenants = await repo.list_all(limit=500)

        for tenant in tenants:
            if tenant_id_filter and tenant.id != tenant_id_filter:
                continue
            if not tenant.is_active:
                continue

            # Scope RLS to this tenant for the product_cache writes below.
            await set_tenant_guc(db, tenant.id)

            store_client = None
            try:
                store_client = _build_client_for_tenant(tenant)
                adapter = _adapter_for_platform(tenant.platform)

                upserted, did_full = await _diff_sync_tenant(
                    db=db,
                    store_client=store_client,
                    adapter=adapter,
                    tenant=tenant,
                )
                total_upserted += upserted
                if did_full:
                    total_full_syncs += 1
                tenants_processed += 1

                # Only purge cache when this tenant actually had changes, so an
                # idle diff run doesn't needlessly cold-start everyone's cache.
                if upserted or did_full:
                    await _invalidate_retrieval_cache(redis_cli, tenant.id)

                logger.info(
                    "Diff sync tenant=%s platform=%s upserted=%d full=%s",
                    tenant.id, tenant.platform, upserted, did_full,
                )
            except Exception as exc:
                logger.warning(
                    "Diff sync failed tenant=%s platform=%s: %s",
                    tenant.id, getattr(tenant, "platform", "?"), exc,
                    exc_info=True,
                )
            finally:
                if store_client is not None:
                    try:
                        await store_client.close()
                    except Exception:
                        pass

        await db.commit()

    await _close_async_redis(redis_cli)

    return {
        "tenants": tenants_processed,
        "upserted": total_upserted,
        "full_syncs": total_full_syncs,
    }


# ── Per-tenant sync logic ─────────────────────────────────────────────────────

async def _sync_tenant(*, db, store_client, adapter, tenant) -> tuple[int, int]:
    platform = (tenant.platform or "shopify").lower()

    if platform == "shopify":
        raw_products = await _shopify_fetch_all(store_client, tenant)
    elif platform == "custom_api":
        raw_products = await _custom_api_fetch_all(store_client)
    else:
        raw_products = await _woo_fetch_all(store_client)

    if not raw_products:
        logger.debug("No products returned for tenant=%s", tenant.id)
        return 0, 0

    logger.info("Fetched %d raw products for tenant=%s", len(raw_products), tenant.id)

    products = adapter.normalize_many(raw_products, tenant_id=tenant.id)
    texts = [
        f"{p.name}. {(p.description or p.short_description)[:300]}"
        for p in products
    ]
    embeddings = await _batch_embed(texts)

    upserted = skipped = 0
    for product, embedding in zip(products, embeddings):
        if not product.platform_id or not product.name:
            skipped += 1
            continue
        try:
            await _upsert_product(db, product, embedding)
            upserted += 1
        except Exception as exc:
            logger.warning(
                "Upsert failed product=%s tenant=%s: %s",
                product.platform_id, tenant.id, exc,
            )
            skipped += 1

    return upserted, skipped


async def _diff_sync_tenant(*, db, store_client, adapter, tenant) -> tuple[int, bool]:
    platform = (tenant.platform or "shopify").lower()
    tenant_id = tenant.id

    cache_count = await _get_cache_count(db, tenant_id)
    platform_count = await _get_platform_count(store_client, platform, tenant)

    if platform_count is not None and cache_count > 0:
        drift = abs(platform_count - cache_count) / max(cache_count, 1)
        if drift > _COUNT_DRIFT_THRESHOLD:
            logger.info(
                "Count drift %.1f%% (platform=%d cache=%d) tenant=%s — full sync",
                drift * 100, platform_count, cache_count, tenant_id,
            )
            upserted, _ = await _sync_tenant(
                db=db, store_client=store_client, adapter=adapter, tenant=tenant,
            )
            await _cleanup_deleted(db, tenant_id)
            return upserted, True

    since_dt = await _get_last_sync_time(db, tenant_id)
    if since_dt is None:
        upserted, _ = await _sync_tenant(
            db=db, store_client=store_client, adapter=adapter, tenant=tenant,
        )
        return upserted, True

    if platform == "shopify":
        raw_products = await _shopify_fetch_diff(store_client, tenant, since_dt)
    elif platform == "custom_api":
        raw_products = await _custom_api_fetch_diff(store_client, since_dt)
    else:
        raw_products = await _woo_fetch_diff(store_client, since_dt)

    if not raw_products:
        return 0, False

    products = adapter.normalize_many(raw_products, tenant_id=tenant_id)
    texts = [
        f"{p.name}. {(p.description or p.short_description)[:300]}"
        for p in products
    ]
    embeddings = await _batch_embed(texts)

    upserted = 0
    for product, embedding in zip(products, embeddings):
        if not product.platform_id or not product.name:
            continue
        try:
            await _upsert_product(db, product, embedding)
            upserted += 1
        except Exception as exc:
            logger.warning(
                "Diff upsert failed product=%s tenant=%s: %s",
                product.platform_id, tenant_id, exc,
            )

    return upserted, False


# ── Per-tenant client + adapter builders ──────────────────────────────────────

def _build_client_for_tenant(tenant) -> Any:
    """Build a store client from tenant DB credentials — never from env vars."""
    platform = (tenant.platform or "shopify").lower()

    if platform == "shopify":
        from ...integrations.shopify.client import ShopifyClient
        return ShopifyClient(
            store_domain=tenant.shopify_domain or "",
            storefront_token=tenant.shopify_storefront_token or "",
            admin_token=tenant.shopify_access_token or "",
            redis_client=None,
        )
    elif platform == "woocommerce":
        from ...integrations.woocommerce.client import WooCommerceClient
        return WooCommerceClient(
            store_url=tenant.woocommerce_store_url or "",
            consumer_key=tenant.woocommerce_consumer_key or "",
            consumer_secret=tenant.woocommerce_consumer_secret or "",
            redis_client=None,
        )
    elif platform == "custom_api":
        from ...integrations.custom_api.client import CustomApiClient
        return CustomApiClient(
            base_url=tenant.custom_api_base_url or "",
            api_key=tenant.custom_api_key or "",
        )
    else:
        raise ValueError(f"Unknown platform '{platform}' for tenant {tenant.id}")


def _adapter_for_platform(platform: str):
    platform = (platform or "shopify").lower()
    if platform == "shopify":
        from ...integrations.adapters import ShopifyAdapter
        return ShopifyAdapter
    elif platform == "custom_api":
        from ...integrations.adapters import CustomAdapter
        return CustomAdapter
    else:
        from ...integrations.adapters import WooAdapter
        return WooAdapter


# ── Platform-specific fetch helpers ──────────────────────────────────────────

async def _shopify_fetch_all(store_client, tenant) -> list[dict]:
    admin_token = tenant.shopify_access_token or ""
    store_domain = tenant.shopify_domain or ""
    api_version = getattr(tenant, "shopify_api_version", None) or "2025-01"

    if admin_token:
        try:
            from ...integrations.shopify.bulk_sync import ShopifyBulkSync
            async with ShopifyBulkSync(
                admin_token=admin_token,
                store_domain=store_domain,
                api_version=api_version,
            ) as bulk:
                nodes = await bulk.fetch_all_products(timeout=_BULK_TIMEOUT)
            if nodes:
                return [store_client._normalize_product_node(n) for n in nodes]
        except Exception as exc:
            logger.warning(
                "Bulk Operations failed for tenant=%s (%s) — paginated fallback",
                tenant.id, exc,
            )

    # Cursor-paginated Storefront fallback — fetches the FULL catalog, not one page.
    if hasattr(store_client, "fetch_all_products_storefront"):
        try:
            products = await store_client.fetch_all_products_storefront(page_size=250)
            if products:
                return products
        except Exception as exc:
            logger.warning(
                "Storefront paginated fetch failed for tenant=%s (%s) — single-page fallback",
                tenant.id, exc,
            )

    # Last-resort single page (only if cursor pagination is unavailable/failed).
    batch = await store_client.search_products(
        query="", limit=_SHOPIFY_PAGE_SIZE, in_stock_only=False,
    )
    return list(batch or [])


async def _woo_fetch_all(store_client) -> list[dict]:
    raw_client = getattr(store_client, "wc", store_client)

    # No cursor crawler available — single best-effort search page.
    if not hasattr(raw_client, "get_products_page"):
        batch = await store_client.search_products(
            query="", limit=_WOO_PAGE_SIZE, in_stock_only=False,
        )
        return list(batch or [])

    all_products: list[dict] = []
    page = 1
    while page <= 200:
        try:
            batch = await raw_client.get_products_page(page=page, per_page=_WOO_PAGE_SIZE)
        except Exception as exc:
            # A transient page error is NOT end-of-catalog. Abort so the sync task
            # retries the whole run, rather than caching a truncated catalog that
            # would silently look complete. (get_products_page already retries
            # internally, so reaching here means a persistent failure.)
            raise RuntimeError(f"WooCommerce sync aborted at page {page}: {exc}") from exc
        if not batch:
            break  # genuine end of pages
        all_products.extend(batch)
        if len(batch) < _WOO_PAGE_SIZE:
            break
        page += 1
    return all_products


async def _custom_api_fetch_all(store_client) -> list[dict]:
    """Paginate custom API using get_products_page() when available.

    Falls back to a single large search_products() call for stores that
    haven't implemented pagination yet.  Logs a warning when hitting the
    ceiling so the merchant knows they need pagination support.
    """
    all_products: list[dict] = []

    if hasattr(store_client, "get_products_page"):
        page = 1
        while page <= 200:   # 200 × 100 = 20 000 product ceiling
            try:
                batch = await store_client.get_products_page(
                    page=page, per_page=_CUSTOM_PAGE_SIZE,
                )
            except Exception as exc:
                logger.warning("Custom API page=%d failed: %s", page, exc)
                break
            if not batch:
                break
            all_products.extend(batch)
            logger.debug(
                "Custom API page=%d fetched=%d total=%d",
                page, len(batch), len(all_products),
            )
            if len(batch) < _CUSTOM_PAGE_SIZE:
                break
            page += 1
        return all_products

    # Fallback — single large request
    _CEILING = 500
    try:
        products = await store_client.search_products(
            query="", limit=_CEILING, in_stock_only=False,
        )
        if len(products or []) >= _CEILING:
            logger.warning(
                "Custom API returned %d products (ceiling hit). Implement "
                "get_products_page() on your API for full pagination.",
                _CEILING,
            )
        return products or []
    except Exception as exc:
        logger.warning("Custom API single-fetch failed: %s", exc)
        return []


async def _shopify_fetch_diff(store_client, tenant, since_dt: datetime) -> list:
    admin_token = tenant.shopify_access_token or ""
    store_domain = tenant.shopify_domain or ""
    api_version = getattr(tenant, "shopify_api_version", None) or "2025-01"

    if not admin_token:
        return []

    since_str = since_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    url = f"https://{store_domain}/admin/api/{api_version}/products.json"
    params: dict = {"limit": 250, "updated_at_min": since_str, "status": "active"}
    headers = {"X-Shopify-Access-Token": admin_token}
    all_products: list = []

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        while url:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            products = resp.json().get("products", [])
            for p in products:
                all_products.append(_normalize_admin_product(p, store_domain))
            url = _parse_next_link(resp.headers.get("Link", ""))
            params = {}

    return all_products


async def _woo_fetch_diff(store_client, since_dt: datetime) -> list:
    since_str = since_dt.strftime("%Y-%m-%dT%H:%M:%S")
    all_products: list = []
    raw_client = getattr(store_client, "wc", store_client)
    if not hasattr(raw_client, "get_products_page"):
        return []
    page = 1
    while page <= 50:
        try:
            batch = await raw_client.get_products_page(
                page=page, per_page=_WOO_PAGE_SIZE, modified_after=since_str,
            )
        except Exception as exc:
            logger.warning("WooCommerce diff page=%d failed: %s", page, exc)
            break
        if not batch:
            break
        all_products.extend(batch)
        if len(batch) < _WOO_PAGE_SIZE:
            break
        page += 1
    return all_products


async def _custom_api_fetch_diff(store_client, since_dt: datetime) -> list[dict]:
    # Custom APIs have no standard modified-time filter.
    # The drift-count check in _diff_sync_tenant triggers a full sync when
    # needed, so returning [] here is the correct safe default.
    return []


# ── Platform product count (for drift check) ─────────────────────────────────

async def _get_platform_count(store_client, platform: str, tenant) -> Optional[int]:
    try:
        if platform == "shopify":
            admin_token = tenant.shopify_access_token or ""
            store_domain = tenant.shopify_domain or ""
            api_version = getattr(tenant, "shopify_api_version", None) or "2025-01"
            if not admin_token:
                return None
            url = f"https://{store_domain}/admin/api/{api_version}/products/count.json"
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    url,
                    params={"status": "active"},
                    headers={"X-Shopify-Access-Token": admin_token},
                )
                resp.raise_for_status()
                return resp.json().get("count")
        elif platform == "custom_api":
            return None  # no standard count endpoint
        else:
            raw_client = getattr(store_client, "wc", store_client)
            if hasattr(raw_client, "get_product_count"):
                return await raw_client.get_product_count()
    except Exception as exc:
        logger.warning("Platform product count failed tenant=%s: %s", tenant.id, exc)
    return None


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _get_cache_count(db, tenant_id: str) -> int:
    result = await db.execute(
        text("SELECT COUNT(*) FROM product_cache WHERE tenant_id = :tid"),
        {"tid": tenant_id},
    )
    return result.scalar() or 0


async def _get_last_sync_time(db, tenant_id: str) -> Optional[datetime]:
    result = await db.execute(
        text("SELECT MAX(cached_at) FROM product_cache WHERE tenant_id = :tid"),
        {"tid": tenant_id},
    )
    value = result.scalar()
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return value


async def _upsert_product(db, product, embedding: Optional[list[float]]) -> None:
    emb_str = (
        "[" + ",".join(str(v) for v in embedding) + "]"
        if embedding else None
    )
    sql = text("""
        INSERT INTO product_cache
            (id, tenant_id, platform_id, name, description,
             price, currency, image_url, in_stock, stock_quantity,
             category_slug, tags, permalink, embedding, cached_at)
        VALUES
            (:id, :tenant_id, :platform_id, :name, :description,
             :price, :currency, :image_url, :in_stock, :stock_quantity,
             :category_slug, :tags, :permalink,
             CAST(:embedding AS vector),
             NOW())
        ON CONFLICT (tenant_id, platform_id)
        DO UPDATE SET
            name           = EXCLUDED.name,
            description    = EXCLUDED.description,
            price          = EXCLUDED.price,
            currency       = EXCLUDED.currency,
            image_url      = EXCLUDED.image_url,
            in_stock       = EXCLUDED.in_stock,
            stock_quantity = EXCLUDED.stock_quantity,
            category_slug  = EXCLUDED.category_slug,
            tags           = EXCLUDED.tags,
            permalink      = EXCLUDED.permalink,
            embedding      = CASE
                                WHEN EXCLUDED.embedding IS NOT NULL
                                    THEN EXCLUDED.embedding
                                -- Re-embed failed but the text changed: drop the
                                -- now-stale vector (BM25 still finds it; it re-embeds
                                -- next run) rather than matching on old text.
                                WHEN product_cache.name IS DISTINCT FROM EXCLUDED.name
                                  OR product_cache.description IS DISTINCT FROM EXCLUDED.description
                                    THEN NULL
                                ELSE product_cache.embedding
                             END,
            cached_at      = NOW()
    """)
    await db.execute(sql, {
        "id":             str(uuid.uuid4()),
        "tenant_id":      product.tenant_id,
        "platform_id":    product.platform_id,
        "name":           product.name[:500],
        "description":    (product.description or product.short_description or "")[:4000],
        "price":          float(product.price),
        "currency":       product.currency or "USD",
        "image_url":      (product.image_url or "")[:2048] or None,
        "in_stock":       product.in_stock,
        "stock_quantity": product.stock_quantity,
        "category_slug":  (product.category_slug or "")[:255] or None,
        "tags":           product.tags,
        "permalink":      product.permalink or None,
        "embedding":      emb_str,
    })


async def _cleanup_deleted(db, tenant_id: str) -> int:
    result = await db.execute(
        text("""
            DELETE FROM product_cache
            WHERE tenant_id = :tid
              AND cached_at < NOW() - INTERVAL '48 hours'
            RETURNING id
        """),
        {"tid": tenant_id},
    )
    deleted = len(result.fetchall())
    if deleted:
        logger.info(
            "Cleaned up %d stale products for tenant=%s", deleted, tenant_id,
        )
    return deleted


# ── Batch embedding ───────────────────────────────────────────────────────────

async def _batch_embed(texts: list[str]) -> list[Optional[list[float]]]:
    try:
        from openai import AsyncOpenAI
        from ...config import settings
        if not settings.openai_api_key:
            return [None] * len(texts)
        # Bounded timeout + no SDK retries: embeddings are OPTIONAL (they power
        # vector search; BM25 text search works without them). A slow/blocked
        # connection to the embeddings API must NEVER hang the whole product
        # sync — on timeout we upsert with NULL embeddings and move on.
        client = AsyncOpenAI(api_key=settings.openai_api_key, timeout=20.0, max_retries=1)
        results: list[Optional[list[float]]] = []
        for i in range(0, len(texts), _EMBED_BATCH):
            batch = texts[i: i + _EMBED_BATCH]
            try:
                resp = await asyncio.wait_for(
                    client.embeddings.create(
                        model="text-embedding-3-small",
                        input=[t[:512] for t in batch],
                    ),
                    timeout=30.0,
                )
                results.extend(d.embedding for d in resp.data)
            except Exception as exc:
                logger.warning("Embed batch %d failed/timed out: %s", i // _EMBED_BATCH, exc)
                results.extend([None] * len(batch))
        return results
    except ImportError:
        return [None] * len(texts)
    except Exception as exc:
        logger.warning("Batch embed setup failed: %s", exc)
        return [None] * len(texts)


# ── Shopify Admin REST normalizer ─────────────────────────────────────────────

def _normalize_admin_product(p: dict, store_domain: str) -> dict:
    variants = p.get("variants") or []
    prices = [float(v.get("price") or 0) for v in variants if v.get("price")]
    compare_prices = [
        float(v.get("compare_at_price") or 0)
        for v in variants if v.get("compare_at_price")
    ]
    price = min(prices) if prices else 0.0
    regular_price = max(compare_prices) if compare_prices else price
    on_sale = regular_price > price > 0
    total_inventory = sum(int(v.get("inventory_quantity") or 0) for v in variants)
    in_stock = any(
        v.get("inventory_management") is None
        or int(v.get("inventory_quantity") or 0) > 0
        for v in variants
    )
    images = p.get("images") or []
    image_url = images[0].get("src", "") if images else ""
    handle = p.get("handle", "")
    permalink = f"https://{store_domain}/products/{handle}" if handle else ""
    options = p.get("options") or []
    attributes = [
        {"name": opt.get("name", ""), "options": opt.get("values") or []}
        for opt in options if opt.get("name")
    ]
    option_names = [opt.get("name", "") for opt in options]
    variations_summary = []
    for v in variants[:10]:
        v_attrs = {}
        for i, name in enumerate(option_names):
            val = v.get(f"option{i + 1}")
            if val:
                v_attrs[name] = val
        variations_summary.append({
            "id": v.get("id"),
            "attributes": v_attrs,
            "price": str(v.get("price") or price),
            "stock_status": "instock" if int(v.get("inventory_quantity") or 0) > 0 else "outofstock",
            "stock_qty": v.get("inventory_quantity"),
        })
    return {
        "id": p.get("id"),
        "name": p.get("title", ""),
        "price": str(price),
        "sale_price": str(price) if on_sale else "",
        "regular_price": str(regular_price),
        "stock_status": "instock" if in_stock else "outofstock",
        "stock_quantity": total_inventory if total_inventory > 0 else None,
        "image_url": image_url,
        "permalink": permalink,
        "short_description": re.sub(r"<[^>]+>", "", p.get("body_html") or "")[:500],
        "attributes": attributes,
        "variations_summary": variations_summary,
        "on_sale": on_sale,
        "tags": [t.strip() for t in (p.get("tags") or "").split(",") if t.strip()],
    }


def _parse_next_link(link_header: str) -> Optional[str]:
    if not link_header:
        return None
    for part in link_header.split(","):
        part = part.strip()
        if 'rel="next"' in part:
            match = re.search(r"<([^>]+)>", part)
            if match:
                return match.group(1)
    return None
