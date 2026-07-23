"""L1 + L2 search cache.

L1 — Exact cache  (Redis, ~3ms, ~70% hit rate)
     Key:   retrieval:{tenant_id}:l1:{cache_key}
     Value: JSON-serialised list[SearchResult]
     TTL:   5 minutes

L2 — Semantic cache  (Redis, ~15ms, ~15% hit rate)
     Stores query embeddings alongside their results.
     On a new query: embed it, find nearest stored embedding via dot-product
     comparison across all L2 keys for that tenant.
     If cosine similarity ≥ SEMANTIC_THRESHOLD → return cached results.
     TTL:   15 minutes

Both layers write-through: a cache miss at L1 triggers L3 search, whose
results are written back to both L1 and L2 before returning.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
_L1_TTL = 300          # 5 minutes
_L2_TTL = 900          # 15 minutes
_SEMANTIC_THRESHOLD = 0.97   # cosine similarity floor for L2 hit. Tightened from
                             # 0.92: attribute-variant queries ("red"/"blue" shoes)
                             # embed ~0.93-0.95 similar and were cross-serving the
                             # wrong product. Attribute queries also bypass L2 in
                             # search.py; this guards the non-attribute long tail.
_L2_MAX_CANDIDATES = 200     # max embeddings to compare per tenant (guards latency)

_L1_PREFIX = "retrieval:{tenant}:l1:{key}"
_L2_EMB_PREFIX = "retrieval:{tenant}:l2:emb:{key}"   # embedding bytes
_L2_RES_PREFIX = "retrieval:{tenant}:l2:res:{key}"   # result JSON
# The L2 index is bucketed by a filter signature so semantic matching only
# compares queries that share the SAME price/stock filters. Without this, "shoes
# under $50" and "shoes under $500" embed near-identically and the cheaper query
# would return the pricier query's cached results (filter bypass).
_L2_INDEX_KEY = "retrieval:{tenant}:l2:index:{fsig}"


def l2_filter_sig(min_price=None, max_price=None, in_stock_only: bool = False) -> str:
    """Deterministic signature of the active filters — buckets the L2 index."""
    parts = []
    if min_price is not None:
        parts.append(f"min{int(min_price)}")
    if max_price is not None:
        parts.append(f"max{int(max_price)}")
    if in_stock_only:
        parts.append("instock")
    return "-".join(parts) or "none"


# ═══════════════════════════════════════════════════════════════════════════════
# L1 — Exact cache
# ═══════════════════════════════════════════════════════════════════════════════

async def l1_get(redis, tenant_id: str, cache_key: str) -> Optional[list[dict]]:
    """Return cached results or None on miss."""
    if redis is None:
        return None
    try:
        key = _L1_PREFIX.format(tenant=tenant_id, key=cache_key)
        raw = await redis.get(key)
        if raw:
            logger.debug("L1 HIT  tenant=%s key=%s", tenant_id, cache_key[:40])
            return json.loads(raw)
    except Exception as exc:
        logger.debug("L1 get error: %s", exc)
    return None


async def l1_set(redis, tenant_id: str, cache_key: str, results: list[dict]) -> None:
    """Write results to L1 cache."""
    if redis is None or not results:
        return
    try:
        key = _L1_PREFIX.format(tenant=tenant_id, key=cache_key)
        await redis.setex(key, _L1_TTL, json.dumps(results))
        logger.debug("L1 SET  tenant=%s key=%s  n=%d", tenant_id, cache_key[:40], len(results))
    except Exception as exc:
        logger.debug("L1 set error: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════════
# Embedding helper — OpenAI text-embedding-3-small  (1536 dims)
# Product-side embeddings include category_slug and tags (see sync_products.py),
# so the query embedding is augmented to match the product format — the query is
# repeated in both "name" and "description" positions to improve cosine alignment.
# Falls back gracefully if OPENAI_API_KEY is missing.
# ═══════════════════════════════════════════════════════════════════════════════

_embed_client: Any = None


def _get_embed_client():
    global _embed_client
    if _embed_client is None:
        try:
            from openai import AsyncOpenAI
            from ....config import settings
            if settings.openai_api_key:
                _embed_client = AsyncOpenAI(api_key=settings.openai_api_key)
        except Exception:
            pass
    return _embed_client


def _augment_query(text: str) -> str:
    """Format the query to match the product embedding text format so cosine
    similarity aligns better.

    Product embeddings now include category_slug and tags (see sync_products.py),
    with the format: \"Name [category] [tags]. Description\".

    To match this format without knowing category/tags at query time, we repeat
    the query in both \"name\" and \"description\" slots:
      query \"formal shoes\" → \"formal shoes. formal shoes search\"

    This works universally across ANY product vertical (apparel, electronics,
    furniture, groceries, etc.) — it doesn't rely on hardcoded keywords.

    The augmented text is only used for embedding — BM25 and caches still
    see the original clean text.
    """
    if not text.strip():
        return text
    text = text.strip()[:200]
    if len(text.split()) < 2:
        return text
    # Format: "name.name description" — mirrors product "Name. Description"
    return f"{text}. {text} search"


async def embed_text(text: str) -> Optional[list[float]]:
    """Return 1536-dim embedding or None if OpenAI unavailable."""
    client = _get_embed_client()
    if client is None:
        return None
    try:
        augmented = _augment_query(text)
        resp = await client.embeddings.create(
            model="text-embedding-3-small",
            input=augmented[:512],    # cap to avoid token overrun
        )
        return resp.data[0].embedding
    except Exception as exc:
        logger.debug("Embedding call failed: %s", exc)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# L2 — Semantic cache
# ═══════════════════════════════════════════════════════════════════════════════

def _cosine(a: list[float], b: list[float]) -> float:
    """Pure-Python cosine similarity. Fast enough for ≤200 cached embeddings."""
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


async def l2_get(
    redis,
    tenant_id: str,
    query_text: str,
    filter_sig: str = "none",
) -> Optional[list[dict]]:
    """Embed the query, compare against stored embeddings, return results on hit.

    Only embeddings stored under the same `filter_sig` (price/stock filters) are
    compared, so a cache hit can never return results from a differently-filtered
    query.
    """
    if redis is None:
        return None

    # Get the embedding for the incoming query
    query_emb = await embed_text(query_text)
    if query_emb is None:
        return None

    try:
        # Retrieve the index of stored L2 keys for this tenant + filter bucket
        index_key = _L2_INDEX_KEY.format(tenant=tenant_id, fsig=filter_sig)
        stored_keys = await redis.smembers(index_key)
        if not stored_keys:
            return None

        # Limit candidates to guard latency
        candidates = list(stored_keys)[:_L2_MAX_CANDIDATES]

        best_sim = 0.0
        best_key: Optional[str] = None

        for sk in candidates:
            emb_key = _L2_EMB_PREFIX.format(tenant=tenant_id, key=sk)
            raw_emb = await redis.get(emb_key)
            if not raw_emb:
                continue
            stored_emb: list[float] = json.loads(raw_emb)
            sim = _cosine(query_emb, stored_emb)
            if sim > best_sim:
                best_sim = sim
                best_key = sk

        if best_sim >= _SEMANTIC_THRESHOLD and best_key:
            res_key = _L2_RES_PREFIX.format(tenant=tenant_id, key=best_key)
            raw_res = await redis.get(res_key)
            if raw_res:
                logger.debug(
                    "L2 HIT  tenant=%s sim=%.3f best_key=%s",
                    tenant_id, best_sim, best_key,
                )
                return json.loads(raw_res)

    except Exception as exc:
        logger.debug("L2 get error: %s", exc)

    return None


async def l2_set(
    redis,
    tenant_id: str,
    cache_key: str,
    query_text: str,
    results: list[dict],
    filter_sig: str = "none",
) -> None:
    """Store embedding + results in L2 cache, bucketed by filter signature."""
    if redis is None or not results:
        return

    query_emb = await embed_text(query_text)
    if query_emb is None:
        return

    try:
        emb_key = _L2_EMB_PREFIX.format(tenant=tenant_id, key=cache_key)
        res_key = _L2_RES_PREFIX.format(tenant=tenant_id, key=cache_key)
        index_key = _L2_INDEX_KEY.format(tenant=tenant_id, fsig=filter_sig)

        async with redis.pipeline(transaction=True) as pipe:
            pipe.setex(emb_key, _L2_TTL, json.dumps(query_emb))
            pipe.setex(res_key, _L2_TTL, json.dumps(results))
            pipe.sadd(index_key, cache_key)
            pipe.expire(index_key, _L2_TTL)
            await pipe.execute()

        logger.debug("L2 SET  tenant=%s key=%s  n=%d", tenant_id, cache_key[:40], len(results))
    except Exception as exc:
        logger.debug("L2 set error: %s", exc)


async def invalidate_tenant(redis, tenant_id: str) -> None:
    """Purge all L1 + L2 cache entries for a tenant (called on product sync)."""
    if redis is None:
        return
    try:
        pattern = f"retrieval:{tenant_id}:*"
        keys = await redis.keys(pattern)
        if keys:
            await redis.delete(*keys)
            logger.info("Cache invalidated: tenant=%s  keys=%d", tenant_id, len(keys))
    except Exception as exc:
        logger.warning("Cache invalidation error: %s", exc)
