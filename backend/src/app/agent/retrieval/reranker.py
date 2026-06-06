"""Reranker — Reciprocal Rank Fusion → Filters → Light Rerank → Top 5.

Pipeline:
  1. Merge BM25 + vector candidates via Reciprocal Rank Fusion (RRF)
  2. Apply hard filters  (price range, in-stock, category)
  3. Light rerank        (exact name match boost, in-stock boost)
  4. Return Top 5

Reciprocal Rank Fusion formula:
  RRF(d) = Σ  1 / (k + rank_i(d))
  where k=60 (standard constant that smooths rank differences)
  and rank_i is the position of document d in each ranked list.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .hybrid_search import RawCandidate
from .normalizer import NormalizedQuery

logger = logging.getLogger(__name__)

_RRF_K = 60          # standard RRF constant
_TOP_N = 5           # final result count


@dataclass
class SearchResult:
    """Final output of the retrieval pipeline, ready for the agent."""
    platform_id: str
    name: str
    description: str
    price: float
    currency: str
    image_url: Optional[str]
    in_stock: bool
    category_slug: Optional[str]
    tags: Optional[str]
    score: float = 0.0       # final RRF + boost score
    source: str = "hybrid"   # "bm25", "vector", or "hybrid"
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id":            self.platform_id,
            "name":          self.name,
            "description":   self.description,
            "price":         self.price,
            "currency":      self.currency,
            "image_url":     self.image_url,
            "in_stock":      self.in_stock,
            "category_slug": self.category_slug,
            "tags":          self.tags,
            "score":         round(self.score, 4),
            "source":        self.source,
        }


def rerank(
    bm25_candidates: list[RawCandidate],
    vec_candidates: list[RawCandidate],
    nq: NormalizedQuery,
    top_n: int = _TOP_N,
) -> list[SearchResult]:
    """Merge two ranked lists → RRF → filter → boost → Top N."""

    # ── 1. Build unified candidate pool keyed by platform_id ─────────────────
    pool: dict[str, RawCandidate] = {}

    for pos, c in enumerate(bm25_candidates, start=1):
        c.bm25_pos = pos
        pool[c.platform_id] = c

    for pos, c in enumerate(vec_candidates, start=1):
        c.vec_pos = pos
        if c.platform_id in pool:
            # Merge vector position into existing candidate
            pool[c.platform_id].vec_pos = pos
            pool[c.platform_id].vec_sim = c.vec_sim
        else:
            pool[c.platform_id] = c

    if not pool:
        return []

    # ── 2. Reciprocal Rank Fusion ─────────────────────────────────────────────
    # A candidate that appeared only in one list gets rank = len(that list) + 1
    # for the missing list (penalised but not zeroed).
    bm25_len = len(bm25_candidates)
    vec_len  = len(vec_candidates)

    scored: list[tuple[float, RawCandidate]] = []
    for pid, c in pool.items():
        bm25_r = c.bm25_pos if c.bm25_pos > 0 else (bm25_len + 1)
        vec_r  = c.vec_pos  if c.vec_pos  > 0 else (vec_len  + 1)
        rrf_score = 1.0 / (_RRF_K + bm25_r) + 1.0 / (_RRF_K + vec_r)
        scored.append((rrf_score, c))

    # ── 3. Hard filters ───────────────────────────────────────────────────────
    filtered: list[tuple[float, RawCandidate]] = []
    for score, c in scored:
        if nq.min_price is not None and c.price < nq.min_price:
            continue
        if nq.max_price is not None and c.price > nq.max_price:
            continue
        if nq.in_stock_only and not c.in_stock:
            continue
        filtered.append((score, c))

    if not filtered:
        # Relax in-stock filter if nothing passed — better to show out-of-stock than nothing
        filtered = [(s, c) for s, c in scored
                    if (nq.min_price is None or c.price >= nq.min_price)
                    and (nq.max_price is None or c.price <= nq.max_price)]

    # ── 4. Light rerank — apply boosts ────────────────────────────────────────
    boosted: list[tuple[float, RawCandidate]] = []
    clean_lower = nq.clean.lower()

    for rrf_score, c in filtered:
        boost = 0.0

        # Exact name match → strong boost
        if clean_lower and clean_lower in c.name.lower():
            boost += 0.05

        # Name starts with query → medium boost
        if clean_lower and c.name.lower().startswith(clean_lower[:10]):
            boost += 0.02

        # In-stock preference (slight)
        if c.in_stock:
            boost += 0.005

        # Vector similarity confidence
        boost += c.vec_sim * 0.01

        boosted.append((rrf_score + boost, c))

    # ── 5. Sort descending and take Top N ─────────────────────────────────────
    boosted.sort(key=lambda x: x[0], reverse=True)
    top = boosted[:top_n]

    # ── 6. Convert to SearchResult ────────────────────────────────────────────
    results = []
    for score, c in top:
        source = "hybrid"
        if c.bm25_pos > 0 and c.vec_pos == 0:
            source = "bm25"
        elif c.vec_pos > 0 and c.bm25_pos == 0:
            source = "vector"

        results.append(SearchResult(
            platform_id=c.platform_id,
            name=c.name,
            description=c.description,
            price=c.price,
            currency=c.currency,
            image_url=c.image_url,
            in_stock=c.in_stock,
            category_slug=c.category_slug,
            tags=c.tags,
            score=score,
            source=source,
        ))

    logger.debug(
        "Rerank: pool=%d filtered=%d top=%d query='%s'",
        len(pool), len(filtered), len(results), nq.clean[:40],
    )
    return results
