"""Deterministic offer rule engine — pure math, NO LLM.

Evaluates a customer cart against the merchant's active rules and returns the
exact totals Aria, the cart widget and Shopify checkout all agree on. Keeping
every price here guarantees zero AI hallucination and price parity end-to-end.

Cart line shape (already normalized across platforms):
    {"platform_product_id": 123, "quantity": 2, "unit_price": 10.5, "name": "Shoe"}

Offer shape — either a dict with the keys below or a ProductOffer ORM object
(use :func:`offer_to_dict` for ORM rows). Combo items carry the trigger and
reward products together:
    combo_items = [{"platform_id": "123", "quantity": 1, "name": "Shoe"},
                   {"platform_id": "456", "quantity": 2, "name": "Watch"}]
    combo_price = 40.0   # the "only this much" bundle price
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _pid(value: Any) -> str:
    """Normalize a platform product id to a comparable string."""
    return str(value or "").strip()


def offer_to_dict(offer: Any) -> Dict[str, Any]:
    """Coerce a ProductOffer ORM row (or already-dict) into a plain dict."""
    if isinstance(offer, dict):
        return offer
    data: Dict[str, Any] = {}
    for col in (
        "id", "platform_id", "product_name", "offer_type", "offer_kind",
        "title", "description", "discount_percent", "discount_amount",
        "combo_items", "combo_price", "bulk_tiers", "max_redemptions",
        "redemption_count", "inventory_threshold", "discount_code",
        "starts_at", "ends_at", "is_active", "priority",
    ):
        data[col] = getattr(offer, col, None)
    return data


def _is_offer_exhausted(offer: Dict[str, Any]) -> bool:
    max_red = offer.get("max_redemptions")
    if max_red is None:
        return False
    return int(offer.get("redemption_count", 0) or 0) >= int(max_red)


def _cart_qty(cart_items: List[Dict[str, Any]], pid: Any) -> int:
    return sum(
        int(i.get("quantity", 0) or 0)
        for i in cart_items
        if _pid(i.get("platform_product_id")) == _pid(pid)
    )


def _cart_line_for(cart_items: List[Dict[str, Any]], pid: Any) -> Optional[Dict[str, Any]]:
    for i in cart_items:
        if _pid(i.get("platform_product_id")) == _pid(pid):
            return i
    return None


def _combo_full_price(cart_items: List[Dict[str, Any]], combo_items: List[Dict[str, Any]]) -> float:
    """Full price of the qualifying combo items already in the cart."""
    total = 0.0
    for item in combo_items or []:
        qty = _cart_qty(cart_items, item.get("platform_id"))
        qty = min(qty, int(item.get("quantity", 1) or 1))
        line = _cart_line_for(cart_items, item.get("platform_id"))
        if line:
            total += qty * _f(line.get("unit_price"))
    return total


def _combo_is_satisfied(cart_items: List[Dict[str, Any]], combo_items: List[Dict[str, Any]]) -> bool:
    for item in combo_items or []:
        if _cart_qty(cart_items, item.get("platform_id")) < int(item.get("quantity", 1) or 1):
            return False
    return bool(combo_items)


def evaluate_combos(
    cart_items: List[Dict[str, Any]],
    combo_offers: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return every satisfied combo offer with its computed discount."""
    applied: List[Dict[str, Any]] = []
    for offer in combo_offers:
        if _is_offer_exhausted(offer):
            continue
        combo_items = offer.get("combo_items")
        if not isinstance(combo_items, list) or not combo_items:
            continue
        if not _combo_is_satisfied(cart_items, combo_items):
            continue
        full_price = _combo_full_price(cart_items, combo_items)
        bundle_price = _f(offer.get("combo_price"))
        savings = round(max(0.0, full_price - bundle_price), 2)
        applied.append({
            "offer_id": offer.get("id"),
            "title": offer.get("title"),
            "offer_kind": "combo",
            "kind": "combo",
            "bundle_items": combo_items,
            "bundle_price": round(bundle_price, 2),
            "full_price": round(full_price, 2),
            "discount": savings,
            "savings": savings,
            "discount_code": offer.get("discount_code"),
        })
    return applied


def evaluate_bulk_tiers(
    cart_items: List[Dict[str, Any]],
    bulk_offers: List[Dict[str, Any]],
    excluded_pids: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """Apply quantity-tier discounts to each matching cart line."""
    applied: List[Dict[str, Any]] = []
    excluded = excluded_pids or set()

    for offer in bulk_offers:
        if _is_offer_exhausted(offer):
            continue
        tiers = offer.get("bulk_tiers")
        if not isinstance(tiers, list) or not tiers:
            continue
        target = offer.get("platform_id")
        for line in cart_items:
            pid = line.get("platform_product_id")
            if target and _pid(target) != _pid(pid):
                continue
            if _pid(pid) in excluded:
                continue
            qty = int(line.get("quantity", 0) or 0)
            applicable = [t for t in tiers if int(t.get("min_qty", 1) or 1) <= qty]
            if not applicable:
                continue
            best = max(applicable, key=lambda t: int(t.get("min_qty", 1) or 1))
            line_total = qty * _f(line.get("unit_price"))
            pct = best.get("discount_percent")
            amt = best.get("discount_amount")
            if pct is not None:
                discount = round(line_total * _f(pct) / 100.0, 2)
            elif amt is not None:
                discount = round(min(line_total, _f(amt) * qty), 2)
            else:
                continue
            if discount > 0:
                applied.append({
                    "offer_id": offer.get("id"),
                    "title": offer.get("title"),
                    "offer_kind": "bulk",
                    "kind": "bulk",
                    "platform_id": pid,
                    "quantity": qty,
                    "line_total": round(line_total, 2),
                    "discount": discount,
                    "savings": discount,
                    "tier": best,
                    "discount_code": offer.get("discount_code"),
                })
    return applied


def evaluate_discounts(
    cart_items: List[Dict[str, Any]],
    discount_offers: List[Dict[str, Any]],
    excluded_pids: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """Apply simple percent/fixed discounts (discount + dead_stock kinds)."""
    applied: List[Dict[str, Any]] = []
    excluded = excluded_pids or set()

    for offer in discount_offers:
        if _is_offer_exhausted(offer):
            continue
        target = offer.get("platform_id")
        if not target:
            continue
        line = _cart_line_for(cart_items, target)
        if not line:
            continue
        pid = line.get("platform_product_id")
        if _pid(pid) in excluded:
            continue
        qty = int(line.get("quantity", 0) or 0)
        line_total = qty * _f(line.get("unit_price"))
        pct = offer.get("discount_percent")
        amt = offer.get("discount_amount")
        if pct is not None:
            discount = round(line_total * _f(pct) / 100.0, 2)
        elif amt is not None:
            discount = round(min(line_total, _f(amt) * qty), 2)
        else:
            continue
        if discount > 0:
            applied.append({
                "offer_id": offer.get("id"),
                "title": offer.get("title"),
                "offer_kind": offer.get("offer_kind") or "discount",
                "kind": offer.get("offer_kind") or "discount",
                "platform_id": pid,
                "quantity": qty,
                "line_total": round(line_total, 2),
                "discount": discount,
                "savings": discount,
                "discount_code": offer.get("discount_code"),
            })
    return applied


def evaluate_cart(
    cart_items: List[Dict[str, Any]],
    offers: List[Any],
) -> Dict[str, Any]:
    """Full cart evaluation.

    Priority order (deterministic): combo > bulk > simple discount, and a line
    covered by an applied combo is excluded from the other passes so a bundle
    price is never double-discounted.

    Returns ``{subtotal, discounts[], savings, total, applied_offers[]}`` where
    each discount entry has ``{title, kind, discount, savings}`` plus offer
    metadata, and ``applied_offers`` are the offer ids redeemed.
    """
    offers = [offer_to_dict(o) for o in offers]
    cart_items = [
        {
            "platform_product_id": i.get("platform_product_id") or i.get("product_id"),
            "quantity": int(i.get("quantity", 1) or 1),
            "unit_price": _f(i.get("unit_price") or i.get("price")),
            "name": i.get("name", ""),
        }
        for i in cart_items
    ]

    subtotal = round(sum(i["quantity"] * i["unit_price"] for i in cart_items), 2)

    combos = evaluate_combos(
        cart_items,
        [o for o in offers if o.get("offer_kind") == "combo"],
    )
    combo_pids: set = set()
    for combo in combos:
        for item in combo.get("bundle_items") or []:
            combo_pids.add(_pid(item.get("platform_id")))

    bulk = evaluate_bulk_tiers(
        cart_items,
        [o for o in offers if o.get("offer_kind") == "bulk"],
        excluded_pids=combo_pids,
    )
    simple = evaluate_discounts(
        cart_items,
        [o for o in offers if o.get("offer_kind") in ("discount", "dead_stock")],
        excluded_pids=combo_pids,
    )

    all_discounts: List[Dict[str, Any]] = combos + bulk + simple
    savings = round(sum(d["discount"] for d in all_discounts), 2)
    total = round(max(0.0, subtotal - savings), 2)

    return {
        "subtotal": subtotal,
        "discounts": all_discounts,
        "savings": savings,
        "total": total,
        "applied_offers": [d.get("offer_id") for d in all_discounts if d.get("offer_id")],
    }
