"""Main entry point for the retrieval layer.

Call:  results = await hybrid_search(tenant_id, query, redis, store_client)

Pipeline (Storefront-primary):
  L0  normalize(query)                  →  NormalizedQuery   (~0.5ms)
  L1  l1_get(cache_key)                 →  hit? return early  (~3ms, TTL 60s)
  LIVE  store_client.search_products()  →  Storefront API (PRIMARY, ~200-500ms)
  L3  l3_search (BM25 + vector + RRF)   →  DB cache FALLBACK (only when the
                                           live Storefront path is unavailable)
  →   l1_set (write-through cache)
  →   return list[SearchResult]
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from .normalizer import normalize, NormalizedQuery
from .cache import l1_get, l1_set
from .reranker import SearchResult, rerank as _rerank
from .hybrid_search import l3_search
from ...integrations.adapters import ShopifyAdapter, WooAdapter, CustomAdapter

logger = logging.getLogger(__name__)


async def hybrid_search(
    tenant_id: str,
    query: str,
    *,
    redis: Any = None,
    db: Optional[AsyncSession] = None,
    store_client: Any = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    in_stock_only: bool = False,
    category_slug: Optional[str] = None,
    size: Optional[str] = None,
    color: Optional[str] = None,
    limit: int = 5,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
) -> list[SearchResult]:
    """
    Full L0→L3 retrieval pipeline.

    Args:
        tenant_id:    Multi-tenant isolation key.
        query:        Raw user query string.
        redis:        aioredis client (None → skip L1/L2).
        db:           AsyncSession (None → skip L3 DB search, use live API).
        store_client: BaseStoreClient (used as live fallback when cache empty).
        min_price:    Optional price floor (overrides query-extracted value).
        max_price:    Optional price ceiling (overrides query-extracted value).
        in_stock_only: Force in-stock filter (overrides query-extracted value).
        category_slug: Optional category filter.
        limit:        Max results to return (default 5).
        sort_by:      Sort field ("price", "title", "newest", "best_selling", "oldest").
        sort_order:   Sort direction ("asc" or "desc").

    Returns:
        list[SearchResult] — ordered by relevance score, highest first.
        If sort_by is set, the live API path applies the requested sort.
    """
    t0 = time.monotonic()

    # ── L0: Normalize ─────────────────────────────────────────────────────────
    nq = normalize(query)

    # Allow callers to override extracted filters
    if min_price is not None:
        nq.min_price = min_price
    if max_price is not None:
        nq.max_price = max_price
    if in_stock_only:
        nq.in_stock_only = True
    if size is not None:
        nq.size = size
    if color is not None:
        nq.color = color

    logger.debug(
        "Search L0: clean='%s' lang=%s min=%.0f max=%.0f stock=%s  (%.1fms)",
        nq.clean, nq.lang,
        nq.min_price or 0, nq.max_price or 0, nq.in_stock_only,
        (time.monotonic() - t0) * 1000,
    )

    # ── L1: Exact cache ────────────────────────────────────────────────────────
    cached = await l1_get(redis, tenant_id, nq.cache_key)
    if cached is not None:
        results = _dicts_to_results(cached)[:limit]
        logger.info(
            "Search L1 HIT  tenant=%s query='%s'  n=%d  (%.1fms)",
            tenant_id, nq.clean[:40], len(results),
            (time.monotonic() - t0) * 1000,
        )
        return results

    # ── LIVE STOREFRONT API — PRIMARY for every query ─────────────────────────
    # Fresh price/stock straight from the store; attribute + price filters are
    # applied client-side by the store client. Raises StorefrontUnavailableError
    # (raise_on_error=True) only when the whole live path is down, never on a
    # genuine empty result.
    live_failed = store_client is None
    results: list[SearchResult] = []
    if store_client is not None:
        try:
            raw = await store_client.search_products(
                query=nq.clean or "",
                min_price=nq.min_price,
                max_price=nq.max_price,
                in_stock_only=nq.in_stock_only,
                size=nq.size,
                color=nq.color,
                limit=limit,
                sort_by=sort_by,
                sort_order=sort_order,
                raise_on_error=True,
            )
            _client_name = type(store_client).__name__
            platform = getattr(store_client, "_platform", None) or (
                "shopify" if _client_name == "ShopifyClient"
                else "custom_api" if _client_name == "CustomApiClient"
                else "woocommerce"
            )
            results = _raw_to_results(raw, platform=platform)
            logger.info(
                "Search LIVE tenant=%s query='%s'  n=%d  (%.1fms)",
                tenant_id, nq.clean[:40], len(results),
                (time.monotonic() - t0) * 1000,
            )
        except Exception as exc:
            live_failed = True
            logger.warning(
                "Search LIVE failed, falling back to DB cache: %s", exc,
            )

    # ── DB CACHE (hybrid_search / pgvector) — FALLBACK ────────────────────────
    # Consulted ONLY when the live Storefront path is unavailable. A live search
    # that returned [] is an honest "no match" and never hits the (stale) cache.
    if live_failed and not results and db is not None:
        try:
            bm25_results, vec_results = await l3_search(db, tenant_id, nq)
            results = _rerank(bm25_results, vec_results, nq, top_n=limit)
            logger.info(
                "Search L3 FALLBACK tenant=%s query='%s'  n=%d  (%.1fms)",
                tenant_id, nq.clean[:40], len(results),
                (time.monotonic() - t0) * 1000,
            )
        except Exception as exc:
            logger.warning("L3 DB fallback failed: %s", exc)
            results = []

    # ── Write-through cache ────────────────────────────────────────────────────
    if results:
        result_dicts = [r.to_dict() for r in results]
        await l1_set(redis, tenant_id, nq.cache_key, result_dicts)

    return results


# ── Helpers ───────────────────────────────────────────────────────────────────

def _dicts_to_results(data: list[dict]) -> list[SearchResult]:
    results = []
    for d in data:
        results.append(SearchResult(
            platform_id=str(d.get("id") or d.get("platform_id", "")),
            name=str(d.get("name", "")),
            description=str(d.get("description", "")),
            price=float(d.get("price", 0)),
            currency=str(d.get("currency", "USD")),
            image_url=d.get("image_url"),
            # Default unknown/absent stock to False, NOT True — defaulting to True
            # bakes in hallucination (presenting unconfirmed products as buyable).
            in_stock=bool(d.get("in_stock") or False),
            category_slug=d.get("category_slug"),
            tags=d.get("tags"),
            score=float(d.get("score", 0)),
            source=str(d.get("source", "cache")),
            permalink=str(d.get("permalink") or ""),
        ))
    return results


def _raw_to_results(raw: list[dict], *, platform: str = "woocommerce") -> list[SearchResult]:
    """Convert raw store API dicts to SearchResult via canonical adapters (live fallback)."""
    if platform == "shopify":
        adapter = ShopifyAdapter
    elif platform == "custom_api":
        adapter = CustomAdapter
    else:
        adapter = WooAdapter
    canonical_products = adapter.normalize_many(raw)
    results = []
    for p in canonical_products:
        results.append(SearchResult(
            platform_id=p.platform_id,
            name=p.name,
            description=p.description or p.short_description,
            price=p.price,
            currency=p.currency,
            image_url=p.image_url,
            in_stock=p.in_stock,
            category_slug=p.category_slug,
            tags=p.tags,
            score=0.0,
            source="live",
            permalink=p.permalink,
        ))
    return results
