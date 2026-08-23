"""Recommendation engine — 4-tier assembler (Combo → Affinity → Dead-Stock → Bestsellers)."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from sqlalchemy import select, and_

from .repository import OfferRepository
from .engine import (
    _combo_is_satisfied,
    _pid,
    _f,
    offer_to_dict,
)
from ..affinity.repository import AffinityRepository
from ..products.models import ProductCache
from ...core.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

# Tactic tags for persona framing (Phase 3)
TACTIC_MAP = {
    "combo": "Perfect Pairing",
    "bulk": "Smart Shopper",
    "affinity": "Perfect Pairing",
    "dead_stock": "Hidden Gem",
    "bestseller": "Smart Shopper",
}


def _is_real_db(db: Any) -> bool:
    """Check if db session has execute method (real SQLAlchemy session vs test mock)."""
    return hasattr(db, "execute") and callable(getattr(db, "execute", None))


async def _briefs_from_cache(
    db, tenant_id: str, platform_ids: List[str]
) -> Dict[str, Dict[str, Any]]:
    """Batched product_cache lookup. Falls back to empty dict on any error (tests use mock DB)."""
    if not platform_ids:
        return {}
    try:
        stmt = select(ProductCache.platform_id, ProductCache.name, ProductCache.price, ProductCache.in_stock).where(
            and_(
                ProductCache.tenant_id == tenant_id,
                ProductCache.platform_id.in_(platform_ids),
            )
        )
        result = await db.execute(stmt)
        return {
            pid: {"platform_id": pid, "name": name or "", "price": float(price or 0), "in_stock": in_stock}
            for pid, name, price, in_stock in result.all()
        }
    except Exception:
        # Mock DB in tests doesn't have execute() — return empty to trigger store fallback
        return {}


async def _enrich_from_store(
    store_client: Any, platform_ids: List[str]
) -> Dict[str, Dict[str, Any]]:
    """Fallback enrichment via store_client.get_product_details (for tests & cache misses)."""
    result = {}
    if not store_client or not platform_ids:
        return result
    for pid in platform_ids:
        try:
            details = await store_client.get_product_details(int(pid))
            result[pid] = {
                "platform_id": _pid(pid),
                "name": details.get("name") or "",
                "price": _f(details.get("price") or details.get("regular_price")),
                "in_stock": details.get("in_stock", True),
            }
        except Exception:
            result[pid] = {"platform_id": _pid(pid), "name": "", "price": "", "in_stock": True}
    return result


async def get_promoted_products_for_prompt(
    tenant_id: str,
    store_client: Any,
    db_session_factory: Any,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Fetch active promoted products with real product data from the store."""
    if not db_session_factory or not tenant_id:
        return []
    db = None
    try:
        db = db_session_factory()
        repo = OfferRepository(db)
        offers = await repo.get_active_promotions(tenant_id, limit=limit)
        if not offers:
            return []

        platform_ids = [str(o.platform_id) for o in offers if o.platform_id]
        briefs = await _briefs_from_cache(db, tenant_id, platform_ids)
        # Fallback to store_client for any missing (tests, cache misses)
        missing_pids = [pid for pid in platform_ids if pid not in briefs]
        if missing_pids and store_client:
            store_briefs = await _enrich_from_store(store_client, missing_pids)
            briefs.update(store_briefs)

        result = []
        for offer in offers:
            brief = briefs.get(str(offer.platform_id), {})
            name = brief.get("name") or offer.product_name or ""
            price = brief.get("price", 0)
            result.append({
                "name": name,
                "price": f"₹{price}" if price else "",
                "offer_title": offer.title,
                "discount_percent": offer.discount_percent,
                "discount_amount": offer.discount_amount,
                "offer_type": offer.offer_type,
                "offer_kind": offer.offer_kind,
                "platform_id": str(offer.platform_id) if offer.platform_id else "",
            })
        return result
    except Exception as exc:
        logger.debug("get_promoted_products failed (non-fatal): %s", exc)
        return []
    finally:
        if db is not None:
            try:
                await db.close()
            except Exception:
                pass


async def _product_brief(store_client: Any, platform_id: Any) -> Dict[str, Any]:
    """Best-effort product lookup (name + price). Never raises. Kept for backward compat."""
    if not store_client:
        return {"platform_id": _pid(platform_id), "name": "", "price": ""}
    try:
        details = await store_client.get_product_details(int(platform_id))
        return {
            "platform_id": _pid(platform_id),
            "name": details.get("name") or "",
            "price": _f(details.get("price") or details.get("regular_price")),
        }
    except Exception:
        return {"platform_id": _pid(platform_id), "name": "", "price": ""}


def _combo_suggestions(
    offers: List[Dict[str, Any]], cart_items: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    suggestions = []
    for offer in offers:
        combo_items = offer.get("combo_items")
        if not isinstance(combo_items, list) or not combo_items:
            continue
        satisfied = _combo_is_satisfied(cart_items, combo_items)
        missing = []
        for item in combo_items:
            have = sum(
                int(i.get("quantity", 0) or 0)
                for i in cart_items
                if _pid(i.get("platform_product_id")) == _pid(item.get("platform_id"))
            )
            need = int(item.get("quantity", 1) or 1)
            if have < need:
                missing.append({
                    "platform_id": _pid(item.get("platform_id")),
                    "name": item.get("name", ""),
                    "quantity": need - have,
                })
        suggestions.append({
            "tier": 1,
            "kind": "combo",
            "offer_id": offer.get("id"),
            "title": offer.get("title"),
            "bundle_price": _f(offer.get("combo_price")),
            "combo_items": combo_items,
            "satisfied": satisfied,
            "missing_items": missing,
            "merchant_boost": offer.get("merchant_boost", 1.0),
            "platform_id": f"combo:{offer.get('id')}",
        })
    return suggestions


def _bulk_suggestions(
    offers: List[Dict[str, Any]], cart_items: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    suggestions = []
    for offer in offers:
        tiers = offer.get("bulk_tiers")
        if not isinstance(tiers, list) or not tiers:
            continue
        target = offer.get("platform_id")
        current_qty = 0
        if target:
            current_qty = sum(
                int(i.get("quantity", 0) or 0)
                for i in cart_items
                if _pid(i.get("platform_product_id")) == _pid(target)
            )
        for tier in tiers:
            min_qty = int(tier.get("min_qty", 1) or 1)
            if current_qty < min_qty:
                suggestions.append({
                    "tier": 1,
                    "kind": "bulk",
                    "offer_id": offer.get("id"),
                    "title": offer.get("title"),
                    "platform_id": _pid(target),
                    "current_qty": current_qty,
                    "next_tier": tier,
                    "add_quantity": min_qty - current_qty,
                    "merchant_boost": offer.get("merchant_boost", 1.0),
                })
                break
    return suggestions


def _dead_stock_suggestions(
    offers: List[Dict[str, Any]], cart_items: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    suggestions = []
    for offer in offers:
        target = offer.get("platform_id")
        if not target:
            continue
        product_name = offer.get("product_name", "")
        suggestions.append({
            "tier": 3,
            "kind": "dead_stock",
            "offer_id": offer.get("id"),
            "title": offer.get("title"),
            "platform_id": _pid(target),
            "name": product_name,
            "product_name": product_name,  # keep for fallback
            "discount_percent": offer.get("discount_percent"),
            "discount_amount": offer.get("discount_amount"),
            "in_cart": any(
                _pid(i.get("platform_product_id")) == _pid(target) for i in cart_items
            ),
            "merchant_boost": offer.get("merchant_boost", 1.0),
        })
    return suggestions


async def assemble_recommendations(
    tenant_id: str,
    cart_items: List[Dict[str, Any]],
    store_client: Any = None,
    db_session_factory: Any = None,
    phase: str = "commitment",
    limit: int = 6,
) -> List[Dict[str, Any]]:
    """4-tier assembler: T1 combos → T2 affinity → T3 dead-stock → T4 bestsellers.

    Returns ordered, deduped, capped suggestions with tier, tactic, and enrichment.
    """
    if not tenant_id or not db_session_factory:
        return []

    db = None
    try:
        db = db_session_factory()

        # ── Fetch active offers ──
        offers = await OfferRepository(db).get_active_offers(tenant_id)
        offers = [offer_to_dict(o) for o in offers]
        if not offers:
            return []

        # ── T1: Combos ──
        t1_combos = _combo_suggestions(
            [o for o in offers if o.get("offer_kind") == "combo"], cart_items
        )
        t1_combos.sort(key=lambda c: (not c["satisfied"], len(c["missing_items"])))

        # ── T1: Bulk ──
        t1_bulk = _bulk_suggestions(
            [o for o in offers if o.get("offer_kind") == "bulk"], cart_items
        )

        # ── T3: Dead-stock ──
        t3_dead = _dead_stock_suggestions(
            [o for o in offers if o.get("offer_kind") == "dead_stock"], cart_items
        )

        # Enrich T1 bulk and T3 dead_stock from cache with store fallback
        enrich_pids = set()
        for s in t1_bulk:
            if s.get("platform_id"):
                enrich_pids.add(s["platform_id"])
        for s in t3_dead:
            if s.get("platform_id"):
                enrich_pids.add(s["platform_id"])
        if enrich_pids and store_client:
            enrich_list = list(enrich_pids)
            briefs = await _briefs_from_cache(db, tenant_id, enrich_list)
            missing_pids = [pid for pid in enrich_list if pid not in briefs]
            if missing_pids:
                store_briefs = await _enrich_from_store(store_client, missing_pids)
                briefs.update(store_briefs)
            for s in t1_bulk:
                if s.get("platform_id"):
                    brief = briefs.get(s["platform_id"], {})
                    s["name"] = brief.get("name", s.get("name", ""))
                    s["unit_price"] = brief.get("price", 0)
            for s in t3_dead:
                if s.get("platform_id"):
                    brief = briefs.get(s["platform_id"], {})
                    s["name"] = brief.get("name", s.get("name", ""))
                    s["price"] = brief.get("price", 0)

        cart_pids = [_pid(i.get("platform_product_id")) for i in cart_items if i.get("platform_product_id")]
        exclude_ids = set(cart_pids)

        # ── T2: Affinity (FBT) ──
        t2_affinity = []
        if _is_real_db(db):
            affinity_repo = AffinityRepository(db)
            try:
                # Phase-gated: discovery = light, commitment/checkout = full
                affinity_limit = 3 if phase == "discovery" else 5
                complements = await affinity_repo.get_top_complements(
                    tenant_id, cart_pids, list(exclude_ids), limit=affinity_limit
                )
                if complements:
                    # Enrich from cache
                    comp_pids = [c["platform_id"] for c in complements]
                    briefs = await _briefs_from_cache(db, tenant_id, comp_pids)
                    # Fallback to store for any missing
                    missing_pids = [pid for pid in comp_pids if pid not in briefs]
                    if missing_pids and store_client:
                        store_briefs = await _enrich_from_store(store_client, missing_pids)
                        briefs.update(store_briefs)
                    for c in complements:
                        brief = briefs.get(c["platform_id"], {})
                        if brief.get("in_stock") is False:
                            continue
                        t2_affinity.append({
                            "tier": 2,
                            "kind": "affinity",
                            "platform_id": c["platform_id"],
                            "name": brief.get("name", ""),
                            "price": brief.get("price", 0),
                            "co_count": c["co_count"],
                            "merchant_boost": 1.0,  # no offer row for affinity
                        })
                        exclude_ids.add(c["platform_id"])
            except Exception as exc:
                logger.debug("Affinity lookup failed (non-fatal): %s", exc)

            # Cold-start fallback for T2
            if not t2_affinity and phase != "discovery":
                try:
                    fallback = await affinity_repo.get_category_cooccurrence_fallback(
                        tenant_id, cart_pids, list(exclude_ids), limit=3
                    )
                    for f in fallback:
                        if f.get("price", 0) > 0:
                            t2_affinity.append({
                                "tier": 2,
                                "kind": "affinity",
                                "platform_id": f["platform_id"],
                                "name": f.get("name", ""),
                                "price": f.get("price", 0),
                                "co_count": 0,
                                "merchant_boost": 1.0,
                            })
                            exclude_ids.add(f["platform_id"])
                except Exception:
                    pass

            # ── T4: Bestsellers ──
            t4_bestsellers = []
            if phase in ("discovery", "commitment"):
                try:
                    bestsellers = await affinity_repo.get_bestsellers(tenant_id, limit=5)
                    for b in bestsellers:
                        if b["platform_id"] in exclude_ids:
                            continue
                        t4_bestsellers.append({
                            "tier": 4,
                            "kind": "bestseller",
                            "platform_id": b["platform_id"],
                            "total_qty": b["total_qty"],
                            "total_revenue": b["total_revenue"],
                            "merchant_boost": 1.0,
                        })
                        exclude_ids.add(b["platform_id"])
                except Exception:
                    pass
        else:
            t4_bestsellers = []

        # ── Merge & Rank ──
        all_suggestions: List[Dict[str, Any]] = []
        all_suggestions.extend(t1_combos)
        all_suggestions.extend(t1_bulk)
        all_suggestions.extend(t2_affinity)
        all_suggestions.extend(t3_dead)
        all_suggestions.extend(t4_bestsellers)

        # Dedupe by platform_id (first tier wins)
        seen = set()
        deduped = []
        for s in all_suggestions:
            pid = s.get("platform_id")
            if pid and pid not in seen:
                seen.add(pid)
                deduped.append(s)

        # Rank: merchant_boost desc, then tier asc, then co_count desc (for affinity)
        deduped.sort(key=lambda s: (
            -(s.get("merchant_boost") or 1.0),
            s.get("tier", 99),
            -(s.get("co_count") or 0),
        ))

        # Cap
        capped = deduped[:limit]

        # Enrich T4 from cache (name/price) with store fallback
        t4_pids = [s["platform_id"] for s in capped if s.get("kind") == "bestseller"]
        if t4_pids:
            briefs = await _briefs_from_cache(db, tenant_id, t4_pids)
            missing_pids = [pid for pid in t4_pids if pid not in briefs]
            if missing_pids and store_client:
                store_briefs = await _enrich_from_store(store_client, missing_pids)
                briefs.update(store_briefs)
            for s in capped:
                if s.get("kind") == "bestseller":
                    brief = briefs.get(s["platform_id"], {})
                    s["name"] = brief.get("name", "")
                    s["price"] = brief.get("price", 0)

        # Add tactic tag for persona framing
        for s in capped:
            s["tactic"] = TACTIC_MAP.get(s.get("kind", ""), "Smart Shopper")

        # Enrich combo items from store (for tests & cache misses)
        combo_items_pids = set()
        for s in capped:
            if s.get("kind") == "combo":
                for item in s.get("combo_items", []):
                    if item.get("platform_id"):
                        combo_items_pids.add(item["platform_id"])
        if combo_items_pids:
            if store_client:
                store_briefs = await _enrich_from_store(store_client, list(combo_items_pids))
            else:
                store_briefs = {}
            for s in capped:
                if s.get("kind") == "combo":
                    for item in s.get("combo_items", []):
                        pid = item.get("platform_id")
                        if pid and pid in store_briefs:
                            brief = store_briefs[pid]
                            if brief.get("name"):
                                item["name"] = brief["name"]
                            item["price"] = brief.get("price", 0)
                        elif pid:
                            item["price"] = item.get("price", "")

        # Ensure dead_stock has name fallback and price="" when store fails
        for s in capped:
            if s.get("kind") == "dead_stock":
                if not s.get("name") and s.get("product_name"):
                    s["name"] = s["product_name"]
                if "price" not in s or s["price"] == 0:
                    s["price"] = ""

        return capped

    except Exception as exc:
        logger.debug("assemble_recommendations failed (non-fatal): %s", exc)
        return []
    finally:
        if db is not None:
            try:
                await db.close()
            except Exception:
                pass


async def get_recommendations_for_cart(
    tenant_id: str,
    cart_items: List[Dict[str, Any]],
    store_client: Any = None,
    db_session_factory: Any = None,
    limit: int = 6,
) -> List[Dict[str, Any]]:
    """Backward-compatible wrapper — delegates to assembler with commitment phase."""
    return await assemble_recommendations(
        tenant_id=tenant_id,
        cart_items=cart_items,
        store_client=store_client,
        db_session_factory=db_session_factory,
        phase="commitment",
        limit=limit,
    )