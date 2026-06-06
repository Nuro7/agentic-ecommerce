"""L3 — Hybrid search: BM25 (tsvector) + Vector (pgvector cosine) in parallel.

Each arm returns Top 20 candidates.
Results are passed to reranker.py for Reciprocal Rank Fusion → Top 5.

BM25 arm  : PostgreSQL tsvector full-text search with ts_rank
Vector arm : pgvector cosine similarity on 1536-dim OpenAI embeddings

Both arms run concurrently via asyncio.gather.
Falls back gracefully when pgvector extension or embeddings are unavailable.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .normalizer import NormalizedQuery
from .cache import embed_text

logger = logging.getLogger(__name__)

_BM25_LIMIT = 20
_VEC_LIMIT = 20


@dataclass
class RawCandidate:
    """A product returned by one search arm before reranking."""
    platform_id: str
    tenant_id: str
    name: str
    description: str
    price: float
    currency: str
    image_url: Optional[str]
    in_stock: bool
    category_slug: Optional[str]
    tags: Optional[str]
    bm25_rank: float = 0.0    # ts_rank score (0–1)
    vec_sim: float = 0.0      # cosine similarity (0–1)
    bm25_pos: int = 0         # position in BM25 list (1-based)
    vec_pos: int = 0          # position in vector list (1-based)
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.platform_id,
            "name": self.name,
            "description": self.description,
            "price": self.price,
            "currency": self.currency,
            "image_url": self.image_url,
            "in_stock": self.in_stock,
            "category_slug": self.category_slug,
            "tags": self.tags,
            "bm25_rank": self.bm25_rank,
            "vec_sim": self.vec_sim,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# BM25 arm — PostgreSQL tsvector full-text search
# ═══════════════════════════════════════════════════════════════════════════════

async def bm25_search(
    db: AsyncSession,
    tenant_id: str,
    nq: NormalizedQuery,
) -> list[RawCandidate]:
    """Full-text search using PostgreSQL tsvector + ts_rank.

    Uses the search_vector column populated by the trigger in migration 0005.
    Falls back to ILIKE if no tsvector matches are found (handles short queries).
    """
    if nq.is_empty():
        return await _browse_all(db, tenant_id, nq)

    # Build tsquery from clean tokens joined with & (AND)
    # plainto_tsquery handles phrase matching safely without injection risk
    try:
        sql = text("""
            SELECT
                platform_id,
                tenant_id,
                name,
                COALESCE(description, '') AS description,
                CAST(price AS FLOAT)      AS price,
                currency,
                image_url,
                in_stock,
                category_slug,
                tags,
                ts_rank(search_vector, plainto_tsquery('english', :query)) AS rank
            FROM product_cache
            WHERE
                tenant_id = :tenant_id
                AND search_vector @@ plainto_tsquery('english', :query)
                AND (:min_price IS NULL OR CAST(price AS FLOAT) >= :min_price)
                AND (:max_price IS NULL OR CAST(price AS FLOAT) <= :max_price)
                AND (:in_stock  IS NULL OR in_stock = :in_stock)
            ORDER BY rank DESC
            LIMIT :limit
        """)

        result = await db.execute(sql, {
            "query":      nq.clean,
            "tenant_id":  tenant_id,
            "min_price":  nq.min_price,
            "max_price":  nq.max_price,
            "in_stock":   True if nq.in_stock_only else None,
            "limit":      _BM25_LIMIT,
        })
        rows = result.mappings().all()

        if rows:
            candidates = [
                RawCandidate(
                    platform_id=r["platform_id"],
                    tenant_id=r["tenant_id"],
                    name=r["name"],
                    description=r["description"],
                    price=float(r["price"]),
                    currency=r["currency"],
                    image_url=r["image_url"],
                    in_stock=bool(r["in_stock"]),
                    category_slug=r.get("category_slug"),
                    tags=r.get("tags"),
                    bm25_rank=float(r["rank"]),
                )
                for r in rows
            ]
            for i, c in enumerate(candidates):
                c.bm25_pos = i + 1
            logger.debug("BM25: %d results for '%s' tenant=%s", len(candidates), nq.clean[:40], tenant_id)
            return candidates

    except Exception as exc:
        logger.warning("BM25 tsvector search failed (%s), falling back to ILIKE", exc)

    # ILIKE fallback — no tsvector matches or extension missing
    return await _ilike_search(db, tenant_id, nq)


async def _ilike_search(
    db: AsyncSession,
    tenant_id: str,
    nq: NormalizedQuery,
) -> list[RawCandidate]:
    """Simple ILIKE fallback when tsvector search returns nothing."""
    try:
        sql = text("""
            SELECT
                platform_id, tenant_id, name,
                COALESCE(description, '') AS description,
                CAST(price AS FLOAT) AS price,
                currency, image_url, in_stock, category_slug, tags
            FROM product_cache
            WHERE
                tenant_id = :tenant_id
                AND (name ILIKE :pattern OR description ILIKE :pattern)
                AND (:min_price IS NULL OR CAST(price AS FLOAT) >= :min_price)
                AND (:max_price IS NULL OR CAST(price AS FLOAT) <= :max_price)
                AND (:in_stock  IS NULL OR in_stock = :in_stock)
            ORDER BY name ASC
            LIMIT :limit
        """)
        result = await db.execute(sql, {
            "pattern":   f"%{nq.clean}%",
            "tenant_id": tenant_id,
            "min_price": nq.min_price,
            "max_price": nq.max_price,
            "in_stock":  True if nq.in_stock_only else None,
            "limit":     _BM25_LIMIT,
        })
        rows = result.mappings().all()
        candidates = [
            RawCandidate(
                platform_id=r["platform_id"],
                tenant_id=r["tenant_id"],
                name=r["name"],
                description=r["description"],
                price=float(r["price"]),
                currency=r["currency"],
                image_url=r["image_url"],
                in_stock=bool(r["in_stock"]),
                category_slug=r.get("category_slug"),
                tags=r.get("tags"),
                bm25_rank=0.5,
            )
            for r in rows
        ]
        for i, c in enumerate(candidates):
            c.bm25_pos = i + 1
        return candidates
    except Exception as exc:
        logger.warning("ILIKE fallback also failed: %s", exc)
        return []


async def _browse_all(
    db: AsyncSession,
    tenant_id: str,
    nq: NormalizedQuery,
) -> list[RawCandidate]:
    """Return top in-stock products when query is empty (browse/discover)."""
    try:
        sql = text("""
            SELECT
                platform_id, tenant_id, name,
                COALESCE(description, '') AS description,
                CAST(price AS FLOAT) AS price,
                currency, image_url, in_stock, category_slug, tags
            FROM product_cache
            WHERE
                tenant_id = :tenant_id
                AND (:in_stock IS NULL OR in_stock = :in_stock)
                AND (:min_price IS NULL OR CAST(price AS FLOAT) >= :min_price)
                AND (:max_price IS NULL OR CAST(price AS FLOAT) <= :max_price)
            ORDER BY cached_at DESC
            LIMIT :limit
        """)
        result = await db.execute(sql, {
            "tenant_id": tenant_id,
            "in_stock":  True if nq.in_stock_only else None,
            "min_price": nq.min_price,
            "max_price": nq.max_price,
            "limit":     _BM25_LIMIT,
        })
        rows = result.mappings().all()
        candidates = [
            RawCandidate(
                platform_id=r["platform_id"],
                tenant_id=r["tenant_id"],
                name=r["name"],
                description=r["description"],
                price=float(r["price"]),
                currency=r["currency"],
                image_url=r["image_url"],
                in_stock=bool(r["in_stock"]),
                category_slug=r.get("category_slug"),
                tags=r.get("tags"),
                bm25_rank=1.0,
            )
            for r in rows
        ]
        for i, c in enumerate(candidates):
            c.bm25_pos = i + 1
        return candidates
    except Exception as exc:
        logger.warning("Browse-all failed: %s", exc)
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# Vector arm — pgvector cosine similarity
# ═══════════════════════════════════════════════════════════════════════════════

async def vector_search(
    db: AsyncSession,
    tenant_id: str,
    nq: NormalizedQuery,
) -> list[RawCandidate]:
    """Cosine similarity search using pgvector.

    Skipped silently when:
      - OpenAI API key missing (no embeddings available)
      - product_cache has no embedding column (migration 0005 not run)
      - query is empty
    """
    if nq.is_empty():
        return []

    query_emb = await embed_text(nq.clean)
    if query_emb is None:
        logger.debug("Vector search skipped — embedding unavailable")
        return []

    try:
        # pgvector operator <=> = cosine distance (1 - similarity)
        # We order ASC (smallest distance = most similar)
        sql = text("""
            SELECT
                platform_id, tenant_id, name,
                COALESCE(description, '') AS description,
                CAST(price AS FLOAT) AS price,
                currency, image_url, in_stock, category_slug, tags,
                1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
            FROM product_cache
            WHERE
                tenant_id = :tenant_id
                AND embedding IS NOT NULL
                AND (:min_price IS NULL OR CAST(price AS FLOAT) >= :min_price)
                AND (:max_price IS NULL OR CAST(price AS FLOAT) <= :max_price)
                AND (:in_stock  IS NULL OR in_stock = :in_stock)
            ORDER BY embedding <=> CAST(:embedding AS vector) ASC
            LIMIT :limit
        """)

        emb_str = "[" + ",".join(str(v) for v in query_emb) + "]"
        result = await db.execute(sql, {
            "embedding": emb_str,
            "tenant_id": tenant_id,
            "min_price": nq.min_price,
            "max_price": nq.max_price,
            "in_stock":  True if nq.in_stock_only else None,
            "limit":     _VEC_LIMIT,
        })
        rows = result.mappings().all()

        candidates = [
            RawCandidate(
                platform_id=r["platform_id"],
                tenant_id=r["tenant_id"],
                name=r["name"],
                description=r["description"],
                price=float(r["price"]),
                currency=r["currency"],
                image_url=r["image_url"],
                in_stock=bool(r["in_stock"]),
                category_slug=r.get("category_slug"),
                tags=r.get("tags"),
                vec_sim=float(r["similarity"]),
            )
            for r in rows
        ]
        for i, c in enumerate(candidates):
            c.vec_pos = i + 1
        logger.debug("Vector: %d results for '%s' tenant=%s", len(candidates), nq.clean[:40], tenant_id)
        return candidates

    except Exception as exc:
        logger.debug("Vector search failed (pgvector may not be installed): %s", exc)
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# L3 entry point — run both arms in parallel
# ═══════════════════════════════════════════════════════════════════════════════

async def l3_search(
    db: AsyncSession,
    tenant_id: str,
    nq: NormalizedQuery,
) -> tuple[list[RawCandidate], list[RawCandidate]]:
    """Run BM25 + vector search concurrently. Returns (bm25_results, vec_results)."""
    bm25_results, vec_results = await asyncio.gather(
        bm25_search(db, tenant_id, nq),
        vector_search(db, tenant_id, nq),
        return_exceptions=False,
    )
    return bm25_results, vec_results
