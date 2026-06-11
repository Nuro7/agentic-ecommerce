"""Main entry point for the retrieval layer.

Call:  results = await hybrid_search(tenant_id, query, redis, db)

Full pipeline:
  L0  normalize(query)            →  NormalizedQuery   (~0.5ms)
  L1  l1_get(cache_key)          →  hit? return early  (~3ms)
  L2  l2_get(query_embedding)    →  hit? return early  (~15ms)
  L3  l3_search(bm25 + vector)   →  parallel search    (~60-150ms)
      rerank(RRF + boost)        →  Top 5
  →   l1_set + l2_set (write-through cache)
  →   return list[SearchResult]

Falls back to live store API when product_cache is empty.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from .normalizer import normalize, NormalizedQuery
from .cache import l1_get, l1_set, l2_get, l2_set, l2_filter_sig
from .hybrid_search import l3_search
from .reranker import rerank, SearchResult
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
    limit: int = 5,
) -> list[SearchResult]:
    """
    Full L0→L3 retrieval pipeline.

    Args:
        tenant_id:    Multi-tenant isolation key.
        query:        Raw user query string.
        redis:        aioredis client (None → skip L1/L2).
        db:           AsyncSession (None → skip L3 DB search).
        store_client: BaseStoreClient (used as live fallback when cache empty).
        min_price:    Optional price floor (overrides query-extracted value).
        max_price:    Optional price ceiling (overrides query-extracted value).
        in_stock_only: Force in-stock filter (overrides query-extracted value).
        category_slug: Optional category filter.
        limit:        Max results to return (default 5).

    Returns:
        list[SearchResult] — ordered by relevance score, highest first.
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

    # ── L2: Semantic cache ─────────────────────────────────────────────────────
    fsig = l2_filter_sig(nq.min_price, nq.max_price, nq.in_stock_only)
    if nq.clean:
        cached = await l2_get(redis, tenant_id, nq.clean, fsig)
        if cached is not None:
            results = _dicts_to_results(cached)[:limit]
            # Promote to L1 for next exact hit
            await l1_set(redis, tenant_id, nq.cache_key, cached)
            logger.info(
                "Search L2 HIT  tenant=%s query='%s'  n=%d  (%.1fms)",
                tenant_id, nq.clean[:40], len(results),
                (time.monotonic() - t0) * 1000,
            )
            return results

    # ── L3: Hybrid search ─────────────────────────────────────────────────────
    results: list[SearchResult] = []

    if db is not None:
        try:
            bm25_hits, vec_hits = await l3_search(db, tenant_id, nq)
            results = rerank(bm25_hits, vec_hits, nq, top_n=limit)
            logger.info(
                "Search L3 MISS tenant=%s query='%s'  bm25=%d vec=%d → top=%d  (%.1fms)",
                tenant_id, nq.clean[:40],
                len(bm25_hits), len(vec_hits), len(results),
                (time.monotonic() - t0) * 1000,
            )
        except Exception as exc:
            logger.warning("L3 DB search failed (%s), falling back to live API", exc)

    # ── Live API fallback (when product_cache is empty or DB unavailable) ──────
    if not results and store_client is not None:
        try:
            raw = await store_client.search_products(
                query=nq.clean or "",
                min_price=nq.min_price,
                max_price=nq.max_price,
                in_stock_only=nq.in_stock_only,
                limit=limit,
            )
            _client_name = type(store_client).__name__
            platform = getattr(store_client, "_platform", None) or (
                "shopify" if _client_name == "ShopifyClient"
                else "custom_api" if _client_name == "CustomApiClient"
                else "woocommerce"
            )
            results = _raw_to_results(raw, platform=platform)
            logger.info(
                "Search LIVE FALLBACK tenant=%s query='%s'  n=%d  (%.1fms)",
                tenant_id, nq.clean[:40], len(results),
                (time.monotonic() - t0) * 1000,
            )
        except Exception as exc:
            logger.error("Live API fallback also failed: %s", exc)

    # ── Write-through cache ────────────────────────────────────────────────────
    if results:
        result_dicts = [r.to_dict() for r in results]
        await l1_set(redis, tenant_id, nq.cache_key, result_dicts)
        if nq.clean:
            await l2_set(redis, tenant_id, nq.cache_key, nq.clean, result_dicts, fsig)

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
            in_stock=bool(d.get("in_stock", True)),
            category_slug=d.get("category_slug"),
            tags=d.get("tags"),
            score=float(d.get("score", 0)),
            source=str(d.get("source", "cache")),
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
        ))
    return results
