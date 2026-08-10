"""Recommendation engine — fetches promoted/dead-stock products for the agent to recommend.

Integrates with the brain: before building the system prompt, the brain calls
get_promoted_products() which returns active offers with product details, injected
into the prompt so the agent naturally recommends them. A second entry point,
get_recommendations_for_cart(), is cart-aware: it returns combo / bulk / dead-stock
suggestions the agent can pitch (deterministic math, exact product ids + names).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .repository import OfferRepository
from .engine import (
    _combo_is_satisfied,
    _pid,
    _f,
    offer_to_dict,
)

logger = logging.getLogger(__name__)


async def get_promoted_products_for_prompt(
    tenant_id: str,
    store_client: Any,
    db_session_factory: Any,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Fetch active promoted products with real product data from the store.

    Returns a list of dicts with: name, price, offer_title, discount.
    Empty list when no active promotions exist or on any error (non-fatal).
    """
    if not db_session_factory or not tenant_id:
        return []
    db = None
    try:
        db = db_session_factory()
        repo = OfferRepository(db)
        offers = await repo.get_active_promotions(tenant_id, limit=limit)
        if not offers:
            return []

        result = []
        for offer in offers:
            try:
                details = await store_client.get_product_details(
                    int(offer.platform_id)
                )
                name = details.get("name") or offer.product_name
                price = details.get("price") or details.get("regular_price") or ""
                result.append({
                    "name": name,
                    "price": f"₹{price}" if price else "",
                    "offer_title": offer.title,
                    "discount_percent": offer.discount_percent,
                    "discount_amount": offer.discount_amount,
                    "offer_type": offer.offer_type,
                    "offer_kind": offer.offer_kind,
                    "platform_id": offer.platform_id,
                })
            except Exception as exc:
                logger.debug("Could not fetch details for promoted product %s: %s",
                             offer.platform_id, exc)
                result.append({
                    "name": offer.product_name,
                    "price": "",
                    "offer_title": offer.title,
                    "discount_percent": offer.discount_percent,
                    "discount_amount": offer.discount_amount,
                    "offer_type": offer.offer_type,
                    "offer_kind": offer.offer_kind,
                    "platform_id": offer.platform_id,
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
    """Best-effort product lookup (name + price). Never raises."""
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
            "kind": "combo",
            "offer_id": offer.get("id"),
            "title": offer.get("title"),
            "bundle_price": _f(offer.get("combo_price")),
            "combo_items": combo_items,
            "satisfied": satisfied,
            "missing_items": missing,
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
                    "kind": "bulk",
                    "offer_id": offer.get("id"),
                    "title": offer.get("title"),
                    "platform_id": _pid(target),
                    "current_qty": current_qty,
                    "next_tier": tier,
                    "add_quantity": min_qty - current_qty,
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
        suggestions.append({
            "kind": "dead_stock",
            "offer_id": offer.get("id"),
            "title": offer.get("title"),
            "platform_id": _pid(target),
            "name": offer.get("product_name", ""),
            "discount_percent": offer.get("discount_percent"),
            "discount_amount": offer.get("discount_amount"),
            "in_cart": any(
                _pid(i.get("platform_product_id")) == _pid(target) for i in cart_items
            ),
        })
    return suggestions


async def get_recommendations_for_cart(
    tenant_id: str,
    cart_items: List[Dict[str, Any]],
    store_client: Any = None,
    db_session_factory: Any = None,
    limit: int = 6,
) -> List[Dict[str, Any]]:
    """Cart-aware offer suggestions for Aria's prompt.

    ``cart_items`` are normalized lines: ``{platform_product_id, quantity,
    unit_price, name}``. Returns ranked suggestions across combo / bulk /
    dead-stock, each with exact platform ids so the agent can add items via the
    existing tools without inventing prices. Never raises; empty on error.
    """
    if not tenant_id or not db_session_factory:
        return []
    db = None
    try:
        db = db_session_factory()
        offers = await OfferRepository(db).get_active_offers(tenant_id)
        offers = [offer_to_dict(o) for o in offers][:limit]
        if not offers:
            return []

        combos = _combo_suggestions(
            [o for o in offers if o.get("offer_kind") == "combo"], cart_items
        )
        bulks = _bulk_suggestions(
            [o for o in offers if o.get("offer_kind") == "bulk"], cart_items
        )
        dead = _dead_stock_suggestions(
            [o for o in offers if o.get("offer_kind") == "dead_stock"], cart_items
        )

        # Rank: satisfied combos first, then partial combos, then bulk, then dead stock
        combos.sort(key=lambda c: (not c["satisfied"], len(c["missing_items"])))
        suggestions: List[Dict[str, Any]] = []
        suggestions.extend(combos)
        suggestions.extend(bulks)
        suggestions.extend(dead)

        # Enrich with live product names/prices (bounded, best-effort)
        enriched: List[Dict[str, Any]] = []
        for s in suggestions[:limit]:
            if s["kind"] == "combo":
                enriched_items = []
                for item in s["combo_items"]:
                    brief = await _product_brief(store_client, item.get("platform_id"))
                    enriched_items.append({
                        "platform_id": brief["platform_id"],
                        "name": brief["name"] or item.get("name", ""),
                        "quantity": item.get("quantity", 1),
                        "price": brief["price"],
                    })
                s["combo_items"] = enriched_items
            elif s["kind"] == "bulk" and s.get("platform_id"):
                brief = await _product_brief(store_client, s["platform_id"])
                s["name"] = brief["name"]
                s["unit_price"] = brief["price"]
            elif s["kind"] == "dead_stock" and s.get("platform_id"):
                brief = await _product_brief(store_client, s["platform_id"])
                s["name"] = brief["name"]
                s["price"] = brief["price"]
            enriched.append(s)
        return enriched
    except Exception as exc:
        logger.debug("get_recommendations_for_cart failed (non-fatal): %s", exc)
        return []
    finally:
        if db is not None:
            try:
                await db.close()
            except Exception:
                pass
