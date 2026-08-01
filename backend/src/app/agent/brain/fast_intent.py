"""Fast deterministic intent handlers — no LLM, ~0ms responses."""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

from .canned import say
from .text_utils import (
    client_platform,
    normalize_discovery_query,
    normalize_availability_query,
    extract_add_query,
    split_compare_terms,
    extract_budget,
    extract_quantity,
    extract_size_color,
    extract_email,
    pick_best_product_match,
    normalize_cart_payload,
    with_actions_alias,
    in_stock,
    safe_int,
    has_shipping_intent,
    has_returns_intent,
    has_payment_intent,
    has_store_info_intent,
    has_cart_view_intent,
    has_cart_nav_intent,
    has_remove_intent,
    has_add_intent,
    has_clear_cart_intent,
    has_quantity_intent,
)
from ..retrieval.search import hybrid_search
from ..retrieval.normalizer import normalize as _normalize_for_size
from ...core.database import AsyncSessionLocal

logger = logging.getLogger(__name__)


def _first_available_variant_id(detail: Dict[str, Any]) -> int:
    """Return the first purchasable variant ID from a product detail dict, or 0.

    Shopify and WooCommerce both expose their variant list under ``variations``
    (Shopify also under ``variations_summary``). Never fall back to the product
    ID here — Shopify's /cart/add.js rejects a product ID with "Cannot find
    variant" (product IDs and variant IDs are distinct namespaces on Shopify).
    """
    if not isinstance(detail, dict):
        return 0
    variations = detail.get("variations") or detail.get("variations_summary") or []
    if not isinstance(variations, list):
        return 0
    for v in variations:
        if isinstance(v, dict) and v.get("id"):
            status = str(v.get("stock_status") or "").lower()
            if status != "outofstock":
                try:
                    return int(v["id"])
                except (TypeError, ValueError):
                    continue
    for v in variations:
        if isinstance(v, dict) and v.get("id"):
            try:
                return int(v["id"])
            except (TypeError, ValueError):
                continue
    return 0


# Backwards-compatible local alias — the matcher now lives in text_utils so the
# brain's fast-intent gate (core.py) can share it.
def _wants_cart_navigation(lower: str) -> bool:
    return has_cart_nav_intent(lower)


def _wants_collections(lower: str) -> bool:
    """True for "show me all the collections/categories in the store" phrasing.

    Deliberately narrow — "catalog"/"show all products" is handled separately by
    product discovery (it should show products), while this only fires for an
    explicit collections/categories request.
    """
    return bool(re.search(
        r"\b(all (?:the )?collections|show (?:me )?collections|browse collections|"
        r"list collections|collections? in (?:the )?(?:store|shop)|open collections|"
        r"browse categories|shop by (?:category|collection)|all the categories|"
        r"^collections?$|^categories$)\b",
        lower,
    ))


async def safe_get_cart(
    tenant_id: str,
    session_id: str,
    *,
    store_client: Any,
    session_service: Any,
) -> Dict[str, Any]:
    try:
        cart = await session_service.get_cart(tenant_id, session_id)
        if cart and not cart.get("is_empty", True):
            return cart
        return {"is_empty": True, "items": [], "total": "₹0", "item_count": 0}
    except Exception as e:
        logger.warning("Cart cache fetch failed: %s", e)
        return {"is_empty": True, "items": [], "total": "₹0", "item_count": 0}


async def run_fast_intent(
    message: str,
    session_id: str,
    language: str,
    store_context: Optional[Dict[str, Any]],
    *,
    tenant_id: str,
    store_client: Any,
    session_service: Any,
    cart_context: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    logger.info("[FLOW] fast_intent ENTER session=%s query=%.60s", session_id, message)
    text = str(message or "")
    lower = text.lower()
    store_name = str((store_context or {}).get("store_name") or "").strip()

    # Per-tenant store config (tenant DB column → env-var fallback). A tenant
    # with NULL columns behaves exactly as before (env text for all tenants).
    from ...modules.tenants.service import get_store_config_for_tenant
    cfg = await get_store_config_for_tenant(tenant_id)

    # If the query is an add-to-cart intent, bail out immediately — the caller
    # (core.py) falls through to handle_add_to_cart which has page_context with
    # the real product_id/variant_id from the PDP. Without this, the generic
    # product search below would match "add to cart" and return a random product.
    if has_add_intent(lower):
        logger.info("[FLOW] fast_intent add_intent detected — returning None for handle_add_to_cart")
        return None

    if has_shipping_intent(lower):
        _shipping = cfg.get("shipping_policy") or os.getenv("STORE_SHIPPING_POLICY", "")
        if _shipping:
            return with_actions_alias({
                "response_text": _shipping,
                "suggested_replies": ["Show products", "Return policy", "Payment methods"],
            })

    if has_returns_intent(lower):
        _returns = cfg.get("returns_policy") or os.getenv("STORE_RETURNS_POLICY", "")
        if _returns:
            return with_actions_alias({
                "response_text": _returns,
                "suggested_replies": ["Show products", "Delivery charges", "Payment methods"],
            })

    if has_payment_intent(lower):
        _payments = cfg.get("payment_methods") or os.getenv("STORE_PAYMENT_METHODS", "")
        if _payments:
            return with_actions_alias({
                "response_text": f"We accept: {_payments}.",
                "suggested_replies": ["Show products", "Delivery charges", "Return policy"],
            })

    if has_store_info_intent(lower):
        _sname = store_name or cfg.get("store_name") or "this store"
        _about = cfg.get("about_text") or os.getenv("STORE_ABOUT", "")
        _shipping = cfg.get("shipping_policy") or os.getenv("STORE_SHIPPING_POLICY", "")
        _returns = cfg.get("returns_policy") or os.getenv("STORE_RETURNS_POLICY", "")
        _payments = cfg.get("payment_methods") or os.getenv("STORE_PAYMENT_METHODS", "")
        _currency = cfg.get("currency_symbol") or os.getenv("STORE_CURRENCY", "₹")
        _support_email = cfg.get("support_email") or ""
        _support_phone = cfg.get("support_phone") or ""
        _hours = cfg.get("business_hours") or ""
        parts = [f"Welcome to {_sname}!"]
        if _about:
            parts.append(_about)
        if _shipping:
            parts.append(_shipping)
        if _returns:
            parts.append(_returns)
        if _payments:
            parts.append(f"We accept: {_payments}.")
        if _support_email or _support_phone:
            _contact = " or ".join(c for c in (_support_email, _support_phone) if c)
            parts.append(f"Contact us: {_contact}.")
        if _hours:
            parts.append(f"Hours: {_hours}.")
        store_reply = " ".join(parts)
        store_info_payload = {
            "store_name": _sname,
            "about": _about,
            "currency": _currency,
            "shipping": _shipping,
            "returns": _returns,
            "payment_methods": _payments,
            "support_email": _support_email,
            "support_phone": _support_phone,
            "business_hours": _hours,
        }
        return with_actions_alias({
            "response_text": store_reply,
            "ui_actions": [{"type": "show_store_info", "payload": store_info_payload}],
            "suggested_replies": ["Show products", "Show my cart", "Browse"],
        })

    # ── Collections / Categories ("show me all the collections in the store") ─
    # A real store concept, not a product search — route it to the store's
    # collections page and name what's available instead of answering
    # "that's not available in the store".
    if _wants_collections(lower):
        base_url = str((store_context or {}).get("url") or "").strip().rstrip("/")
        _platform = client_platform(store_client)
        collections_url = (base_url or "") + (
            "/collections" if _platform != "woocommerce" else "/product-category"
        )
        names = []
        try:
            categories = await store_client.get_categories()
            for c in (categories or []):
                if isinstance(c, dict) and c.get("name"):
                    names.append(str(c["name"]))
        except Exception as _col_exc:
            logger.warning("get_categories failed for collections intent: %s", _col_exc)
        if names:
            listed = ", ".join(names[:6])
            more = f", and {len(names) - 6} more" if len(names) > 6 else ""
            response_text = f"We have {len(names)} collections: {listed}{more}. Opening the collections page for you."
        else:
            response_text = "Opening the collections page for you — browse by category there."
        return with_actions_alias({
            "response_text": response_text,
            "ui_actions": [{"type": "redirect", "payload": {"url": collections_url, "reason": "collections"}}],
            "suggested_replies": ["Show all products", "Show my cart", "Back to home"],
        })

    if _wants_cart_navigation(lower):
        cart = cart_context if (cart_context and isinstance(cart_context, dict) and cart_context.get("items")) else await safe_get_cart(tenant_id, session_id, store_client=store_client, session_service=session_service)
        cart_url = str((store_context or {}).get("cart_url") or "/cart")
        # Render inline AND navigate the storefront to the real cart page, so
        # "go to cart" actually moves the page (the #7 indication-driven UX).
        return with_actions_alias({
            "response_text": say(language, "cart_opened"),
            "ui_actions": [
                {"type": "show_cart", "payload": {"cart": normalize_cart_payload(cart)}},
                {"type": "redirect", "payload": {"url": cart_url, "reason": "cart"}},
            ],
            "suggested_replies": ["Checkout now", "Show products"],
        })

    # ── Clear cart ──────────────────────────────────────────────
    # Checked BEFORE has_cart_view_intent: "clear my cart" contains "my cart"
    # which would otherwise short-circuit into a plain cart view.
    if has_clear_cart_intent(lower):
        cart = cart_context if (cart_context and isinstance(cart_context, dict) and cart_context.get("items")) else await safe_get_cart(tenant_id, session_id, store_client=store_client, session_service=session_service)
        items = cart.get("items") if isinstance(cart.get("items"), list) else []
        if not items:
            return with_actions_alias({
                "response_text": "Your cart is already empty.",
                "ui_actions": [],
                "suggested_replies": ["Show products"],
            })
        mutate_actions = []
        for item in items:
            k = item.get("cart_item_key") or item.get("key") or item.get("variant_id") or item.get("id")
            if k:
                mutate_actions.append({"type": "mutate_cart", "payload": {"cart_item_key": str(k), "quantity": 0}})
        mutate_actions.append({"type": "cart_updated", "payload": {}})
        return with_actions_alias({
            "response_text": "Cleared your cart.",
            "ui_actions": mutate_actions,
            "suggested_replies": ["Show products", "Show my cart"],
        })

    # ── Remove from cart ────────────────────────────────────────
    # Checked BEFORE has_cart_view_intent: "remove nike from my cart" contains
    # "my cart" which would otherwise short-circuit into a plain cart view.
    if has_remove_intent(lower):
        cart = cart_context if (cart_context and isinstance(cart_context, dict) and cart_context.get("items")) else await safe_get_cart(tenant_id, session_id, store_client=store_client, session_service=session_service)
        items = cart.get("items") if isinstance(cart.get("items"), list) else []
        if not items:
            return with_actions_alias({
                "response_text": "Removed from your cart.",
                "ui_actions": [{"type": "remove_from_cart", "payload": {}}],
                "suggested_replies": ["Show products", "Show my cart"],
            })

        target = None
        ordinal_idx = None
        if re.search(r"\b(first|1st)\b", lower):
            ordinal_idx = 0
        elif re.search(r"\b(second|2nd)\b", lower):
            ordinal_idx = 1
        elif re.search(r"\b(third|3rd)\b", lower):
            ordinal_idx = 2
        elif re.search(r"\b(last)\b", lower):
            ordinal_idx = len(items) - 1
        if ordinal_idx is not None and ordinal_idx < len(items):
            target = items[ordinal_idx]
        if target is None:
            target_name = re.sub(r"\b(remove|delete|item|product|from|my|cart|the|this|that)\b", "", lower).strip()
            if target_name:
                best_match = None
                best_score = 0
                for item in items:
                    name = str(item.get("name") or item.get("title") or "").lower()
                    score = len(set(target_name.split()) & set(name.split()))
                    if score > best_score:
                        best_score = score
                        best_match = item
                if best_match and best_score >= 1:
                    target = best_match
        if target is None:
            target = items[-1]

        name = target.get("name", "item") or "item"
        item_key = target.get("cart_item_key") or target.get("key") or target.get("variant_id") or target.get("id")
        payload = {"cart_item_key": str(item_key)} if item_key else {}
        return with_actions_alias({
            "response_text": f"Removed {name} from your cart.",
            "ui_actions": [{"type": "remove_from_cart", "payload": payload}],
            "suggested_replies": ["Show products", "Checkout"],
        })

    if has_cart_view_intent(lower):
        cart = cart_context if (cart_context and isinstance(cart_context, dict) and cart_context.get("items")) else await safe_get_cart(tenant_id, session_id, store_client=store_client, session_service=session_service)
        return with_actions_alias({
            "response_text": say(language, "cart_opened"),
            "ui_actions": [{"type": "show_cart", "payload": {"cart": normalize_cart_payload(cart)}}],
            "suggested_replies": ["Checkout now", "Show products"],
        })

    # ── Quantity update ─────────────────────────────────────────
    if has_quantity_intent(lower):
        cart = cart_context if (cart_context and isinstance(cart_context, dict) and cart_context.get("items")) else await safe_get_cart(tenant_id, session_id, store_client=store_client, session_service=session_service)
        items = cart.get("items") if isinstance(cart.get("items"), list) else []
        if not items:
            return with_actions_alias({
                "response_text": "Your cart is empty.",
                "ui_actions": [],
                "suggested_replies": ["Show products"],
            })

        ordinal_idx = None
        if re.search(r"\b(first|1st)\b", lower):
            ordinal_idx = 0
        elif re.search(r"\b(second|2nd)\b", lower):
            ordinal_idx = 1
        elif re.search(r"\b(third|3rd)\b", lower):
            ordinal_idx = 2

        target = None
        if ordinal_idx is not None and ordinal_idx < len(items):
            target = items[ordinal_idx]
        if target is None:
            target_name = re.sub(r"\b(increase|decrease|reduce|change|update|adjust|quantity|qty|of|the|item|product|to|by|from|my|cart)\b", "", lower).strip()
            if target_name:
                best_match = None
                best_score = 0
                for item in items:
                    name = str(item.get("name") or item.get("title") or "").lower()
                    score = len(set(target_name.split()) & set(name.split()))
                    if score > best_score:
                        best_score = score
                        best_match = item
                if best_match and best_score >= 1:
                    target = best_match
        if target is None:
            target = items[-1]

        current_qty = int(target.get("quantity", 1))
        is_increase = bool(re.search(r"\b(increase|add more|add another|add one more|more|up|raise|extra)\b", lower))
        is_decrease = bool(re.search(r"\b(decrease|reduce|less|down|lower|fewer)\b", lower))
        explicit_qty = None
        qty_match = re.search(r"\bto\s+(\d+)\b", lower)
        if qty_match:
            explicit_qty = int(qty_match.group(1))

        if explicit_qty is not None:
            new_qty = explicit_qty
        elif is_decrease:
            new_qty = max(0, current_qty - 1)
        elif is_increase:
            new_qty = current_qty + 1
        else:
            new_qty = current_qty

        item_key = target.get("cart_item_key") or target.get("key") or target.get("variant_id") or target.get("id")
        name = target.get("name", "item") or "item"

        if new_qty <= 0:
            payload = {"cart_item_key": str(item_key)} if item_key else {}
            return with_actions_alias({
                "response_text": f"Removed {name} from your cart.",
                "ui_actions": [{"type": "remove_from_cart", "payload": payload}],
                "suggested_replies": ["Show products", "Show my cart"],
            })
        elif new_qty == current_qty:
            return with_actions_alias({
                "response_text": f"{name} is already at quantity {current_qty}.",
                "ui_actions": [],
                "suggested_replies": ["Show products", "Show my cart"],
            })
        else:
            payload = {"cart_item_key": str(item_key), "quantity": new_qty} if item_key else {}
            return with_actions_alias({
                "response_text": f"Updated {name} to quantity {new_qty}.",
                "ui_actions": [{"type": "mutate_cart", "payload": payload}],
                "suggested_replies": ["Show products", "Show my cart"],
            })

    # Browse / show products fallback (only runs as LLM fallback)
    browse_tokens = [
        "show products", "show best", "best sellers", "bestsellers",
        "browse", "what do you have", "what products", "show me products",
        "show items", "what's available", "what is available",
        "show all", "products", "items available",
        "what are the available", "available product", "available items",
        "what have you got", "what you have", "what do you sell",
        "what can i buy", "see all", "see products", "list products",
    ]
    if any(token in lower for token in browse_tokens) or lower.strip() in ("browse", "products", "shop"):
        try:
            products = await store_client.search_products(query="", in_stock_only=False, limit=6)
            products = [p for p in (products or []) if isinstance(p, dict)]
            if products:
                first = products[0]
                name = first.get("name", "")
                price = first.get("price") or first.get("regular_price") or ""
                price_str = f"₹{price}" if price else ""
                reply = f"{name}{(', ' + price_str) if price_str else ''}. Want me to tell you more, or check size options?"
                return with_actions_alias({
                    "response_text": reply,
                    "ui_actions": [{"type": "show_products", "payload": {"products": [first]}}],
                    "suggested_replies": ["Tell me more", "Add to cart", "Show my cart"],
                })
        except Exception:
            pass

    # Ordinal/anaphoric navigation bail-out — "first one", "second", "that one",
    # "take the first one", etc. are navigation references that append_live_navigation
    # in text_utils.py handles by redirecting to the correct product page. If we let
    # them hit the generic search fallback below, normalize_discovery_query searches
    # for the literal phrase and returns a random hallucinated product.
    _ordinal_nav = re.search(
        r"\b(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th|"
        r"that one|this one|the one|next one|previous one|last one)\b", lower
    )
    if _ordinal_nav and not has_add_intent(lower):
        # Only return None for pure navigation — add-intent is handled by
        # _resolve_product_for_add in the caller
        logger.info("[FLOW] fast_intent ordinal/nav bail-out for: %.60s", text)
        return None

    # Generic product search fallback
    try:
        query = normalize_discovery_query(text)
        if query.strip():
            products = await store_client.search_products(query=query, in_stock_only=False, limit=5)
            products = [p for p in (products or []) if isinstance(p, dict)]
            if products:
                first = products[0]
                name = first.get("name", "")
                price = first.get("price") or first.get("regular_price") or ""
                price_str = f"₹{price}" if price else ""
                reply = f"{name}{(', ' + price_str) if price_str else ''}. Want me to tell you more, or shall I check size options?"
                return with_actions_alias({
                    "response_text": reply,
                    "ui_actions": [{"type": "show_products", "payload": {"products": [first]}}],
                    "suggested_replies": ["Tell me more", "Add to cart", "Show my cart"],
                })
            else:
                all_products = await store_client.search_products(query="", in_stock_only=False, limit=4)
                all_products = [p for p in (all_products or []) if isinstance(p, dict)]
                if all_products:
                    names = ", ".join(p.get("name", "") for p in all_products[:3] if p.get("name"))
                    reply = f"I couldn't find that exactly, but we have {names} and more. Want me to show you?"
                    return with_actions_alias({
                        "response_text": reply,
                        "ui_actions": [{"type": "show_products", "payload": {"products": all_products}}],
                        "suggested_replies": ["Show products", "Show my cart"],
                    })
    except Exception:
        pass

    return None


async def handle_product_discovery(
    message: str,
    lower: str,
    language: str,
    tenant_id: str = "_dev",
    *,
    store_client: Any,
) -> Dict[str, Any]:
    min_price, max_price = extract_budget(lower)
    query = normalize_discovery_query(message)
    wants_all = any(token in lower for token in [
        "all products", "all items", "entire catalog", "full catalog",
        "list all", "show all", "catalog",
    ])
    limit = 24 if wants_all or not query else 8
    in_stock_only = False if wants_all or not query else ("out of stock" not in lower)
    
    # normalize_discovery_query strips size/price/colour, so re-extract them from
    # the raw message and pass them as structured filters.
    nq_raw = _normalize_for_size(message)
    size = nq_raw.size
    color = nq_raw.color
    if min_price is None:
        min_price = nq_raw.min_price
    if max_price is None:
        max_price = nq_raw.max_price
    # Use hybrid_search (L3: BM25 + vector + RRF) instead of live store API
    async with AsyncSessionLocal() as db:
        search_results = await hybrid_search(
            tenant_id=tenant_id,
            query=query,
            redis=None,
            db=db,
            store_client=store_client,
            min_price=min_price,
            max_price=max_price,
            in_stock_only=in_stock_only,
            size=size,
            color=color,
            limit=limit,
        )
    
    # Convert SearchResult objects to dict format
    products = []
    for r in search_results:
        products.append({
            "id": r.platform_id,
            "name": r.name,
            "description": r.description,
            "price": r.price,
            "currency": r.currency,
            "image_url": r.image_url,
            "in_stock": r.in_stock,
            "category_slug": r.category_slug,
            "tags": r.tags,
            "permalink": r.permalink,
            "short_description": r.description[:200] if r.description else "",
        })
    
    if not products and query and not wants_all:
        words = query.split()
        if len(words) > 1:
            for i in range(1, len(words)):
                shorter = " ".join(words[i:])
                async with AsyncSessionLocal() as db:
                    search_results = await hybrid_search(
                        tenant_id=tenant_id,
                        query=shorter,
                        redis=None,
                        db=db,
                        store_client=store_client,
                        min_price=min_price,
                        max_price=max_price,
                        in_stock_only=in_stock_only,
                        limit=limit,
                    )
                products = []
                for r in search_results:
                    products.append({
                        "id": r.platform_id,
                        "name": r.name,
                        "description": r.description,
                        "price": r.price,
                        "currency": r.currency,
                        "image_url": r.image_url,
                        "in_stock": r.in_stock,
                        "category_slug": r.category_slug,
                        "tags": r.tags,
                        "permalink": r.permalink,
                        "short_description": r.description[:200] if r.description else "",
                    })
                if products:
                    break
    if not products and query and not wants_all:
        return with_actions_alias({
            "response_text": f"I couldn't find anything matching '{query}' in this store. Try a different search or browse our catalog.",
            "ui_actions": [],
            "suggested_replies": ["Show all products", "Browse categories", "Show my cart"],
        })
    if not products:
        async with AsyncSessionLocal() as db:
            search_results = await hybrid_search(
                tenant_id=tenant_id,
                query="",
                redis=None,
                db=db,
                store_client=store_client,
                min_price=min_price,
                max_price=max_price,
                in_stock_only=False,
                limit=24,
            )
        products = []
        for r in search_results:
            products.append({
                "id": r.platform_id,
                "name": r.name,
                "description": r.description,
                "price": r.price,
                "currency": r.currency,
                "image_url": r.image_url,
                "in_stock": r.in_stock,
                "category_slug": r.category_slug,
                "tags": r.tags,
                "permalink": r.permalink,
                "short_description": r.description[:200] if r.description else "",
            })
    if not products:
        return with_actions_alias({
            "response_text": say(language, "no_products"),
            "ui_actions": [],
            "suggested_replies": ["Show products", "Show my cart"],
        })

    products.sort(key=lambda p: (p.get("in_stock", False), 0), reverse=True)
    best = pick_best_product_match(lower, products)
    if best and best in products:
        products.remove(best)
        products.insert(0, best)
    products = products[:6]

    # Build a concise, natural voice summary (top 3-4 recommendation names).
    top_products = products[:4]
    product_descriptions = []
    for p in top_products:
        pname = str(p.get("name") or "").strip()
        if pname:
            product_descriptions.append(pname)

    name = products[0].get("name", "")
    price = products[0].get("price", "")

    is_sold_out = all(not in_stock(p) for p in products[:3])

    if is_sold_out:
        alt_query = str(products[0].get("name") or query or "")
        async with AsyncSessionLocal() as db:
            search_results = await hybrid_search(
                tenant_id=tenant_id,
                query=alt_query,
                redis=None,
                db=db,
                store_client=store_client,
                min_price=min_price,
                max_price=max_price,
                in_stock_only=True,
                limit=4,
            )
        alternatives = []
        for r in search_results:
            alternatives.append({
                "id": r.platform_id,
                "name": r.name,
                "description": r.description,
                "price": r.price,
                "currency": r.currency,
                "image_url": r.image_url,
                "in_stock": r.in_stock,
                "category_slug": r.category_slug,
                "tags": r.tags,
                "permalink": r.permalink,
                "short_description": r.description[:200] if r.description else "",
            })
        if alternatives:
            _alts = ", ".join(product_descriptions[:2]) if product_descriptions else "these"
            response = f"{name} is currently out of stock. I found similar options like {_alts} that are available."
            actions = [
                {"type": "show_products", "payload": {"products": products}},
                {"type": "show_products", "payload": {"products": alternatives}},
            ]
            suggested = ["Show options", "Add to cart", "Show my cart"]
            last_ids = [p.get("id") for p in (products + alternatives) if p.get("id")]
        else:
            response = f"{name} is currently out of stock, and I couldn't find similar alternatives. Check back later or browse our catalog."
            actions = [
                {"type": "show_products", "payload": {"products": products}},
            ]
            suggested = ["Show all products", "Browse categories", "Show my cart"]
            last_ids = [p.get("id") for p in products if p.get("id")]
    else:
        _lead = product_descriptions[0] if product_descriptions else "our top picks"
        response = f"I found {len(products)} great matching options for you, starting with {_lead}. Which one would you like me to open?"
        actions = [
            {"type": "show_products", "payload": {"products": products}},
        ]
        suggested = ["Show options", "Add to cart", "Show my cart"]
        last_ids = [p.get("id") for p in products if p.get("id")]

    return with_actions_alias({
        "response_text": response,
        "ui_actions": actions,
        "suggested_replies": suggested,
        "last_products": last_ids,
    })


async def handle_buy_intent(
    message: str,
    lower: str,
    session_id: str,
    language: str,
    *,
    store_client: Any,
) -> Optional[Dict[str, Any]]:
    query = re.sub(
        r"\b(i want to|i'd like to|i would like to|want to|i want|i'll take|get me a?|buy me a?|buy|purchase|order)\b",
        "", message, flags=re.IGNORECASE,
    ).strip()
    query = re.sub(r"\s+", " ", query).strip()
    if not query:
        return None

    async with AsyncSessionLocal() as db:
        search_results = await hybrid_search(
            tenant_id=tenant_id,
            query=query,
            redis=None,
            db=db,
            store_client=store_client,
            in_stock_only=False,
            limit=4,
        )
    products = []
    for r in search_results:
        products.append({
            "id": r.platform_id,
            "name": r.name,
            "description": r.description,
            "price": r.price,
            "currency": r.currency,
            "image_url": r.image_url,
            "in_stock": r.in_stock,
            "category_slug": r.category_slug,
            "tags": r.tags,
            "permalink": r.permalink,
            "short_description": r.description[:200] if r.description else "",
        })
    if not products:
        return None

    products.sort(key=lambda p: (p.get("in_stock", False), 0), reverse=True)
    product = pick_best_product_match(query, products) or products[0]
    product_id = product.get("id")
    name = product.get("name", "")
    price = product.get("price", "")
    price_text = f"₹{price}" if price else ""

    actions: List[Dict[str, Any]] = [{"type": "show_products", "payload": {"products": [product]}}]
    if product_id:
        actions.append({"type": "show_variant_picker", "payload": {"product_id": product_id}})

    return with_actions_alias({
        "response_text": f"{name}{', ' + price_text if price_text else ''}. Let me pull up the options for you.",
        "ui_actions": actions,
        "suggested_replies": ["Add to cart", "Show details", "Show my cart"],
        "last_products": [p.get("id") for p in products if p.get("id")],
    })


async def handle_buy_now(
    message: str,
    lower: str,
    session_id: str,
    active_recommendations: List[Any],
    language: str,
    page_context: Optional[Dict[str, Any]] = None,
    *,
    store_client: Any,
    tenant_id: str = "",
    session_service: Any = None,
) -> Optional[Dict[str, Any]]:
    """
    Handle 'Buy now' intent: add active variant to cart and redirect to checkout.
    """
    product = await _resolve_product_for_add(
        message=message,
        lower=lower,
        session_id=session_id,
        active_recommendations=active_recommendations,
        page_context=page_context or {},
        store_client=store_client,
        tenant_id=tenant_id,
        session_service=session_service,
    )
    if not product or not product.get("id"):
        return with_actions_alias({
            "response_text": say(language, "ask_add_which"),
            "ui_actions": [],
            "suggested_replies": ["Show products"],
        })

    variant_id = product.get("variant_id") or 0
    if not variant_id:
        try:
            _buy_detail = await store_client.get_product_details(int(product["id"]))
            variant_id = _first_available_variant_id(_buy_detail)
        except Exception:
            pass
    product_id = int(product["id"])
    name = product.get("name", "Product")
    handle = product.get("handle", "")
    permalink = product.get("permalink", "")

    # Build UI actions: add_to_cart then redirect_checkout
    ui_actions = [
        {
            "type": "add_to_cart",
            "payload": {
                "product_id": product_id,
                "variant_id": int(variant_id),
                "quantity": 1,
                "handle": handle,
                "permalink": permalink,
            },
        },
        {
            "type": "redirect_checkout",
            "payload": {
                "reason": "buy_now",
                "url": "/checkout",
                "delay_ms": 800,
            },
        },
    ]

    response_text = f"Adding {name} to your cart and taking you to checkout."
    suggested_replies = ["Continue shopping", "View cart"]

    return with_actions_alias({
        "response_text": response_text,
        "ui_actions": ui_actions,
        "suggested_replies": suggested_replies,
        "last_products": [product_id],
    })


async def handle_availability(
    message: str,
    lower: str,
    last_products: List[Any],
    language: str,
    *,
    store_client: Any,
    tenant_id: str = "",
) -> Optional[Dict[str, Any]]:
    size, color = extract_size_color(lower)
    query = normalize_availability_query(message)

    product: Optional[Dict[str, Any]] = None
    if query:
        async with AsyncSessionLocal() as db:
            search_results = await hybrid_search(
                tenant_id=tenant_id,
                query=query,
                redis=None,
                db=db,
                store_client=store_client,
                in_stock_only=False,
                limit=6,
            )
        rows = []
        for r in search_results:
            rows.append({
                "id": r.platform_id,
                "name": r.name,
                "description": r.description,
                "price": r.price,
                "currency": r.currency,
                "image_url": r.image_url,
                "in_stock": r.in_stock,
                "category_slug": r.category_slug,
                "tags": r.tags,
                "permalink": r.permalink,
                "short_description": r.description[:200] if r.description else "",
            })
        if rows:
            rows.sort(key=lambda p: (p.get("in_stock", False), 0), reverse=True)
            product = pick_best_product_match(query, rows)
    elif last_products:
        _lp0 = last_products[0]
        _lp_id = _lp0.get("id") if isinstance(_lp0, dict) else _lp0
        if _lp_id:
            detail = await store_client.get_product_details(int(_lp_id))
            product = {
                "id": detail.get("id"),
                "name": detail.get("name"),
                "price": detail.get("price"),
                "stock_status": detail.get("stock_status"),
            }

    if not product or not product.get("id"):
        return with_actions_alias({
            "response_text": say(language, "ask_product_for_stock"),
            "ui_actions": [],
            "suggested_replies": ["Show products"],
        })

    attributes: Optional[Dict[str, str]] = None
    if size or color:
        attributes = {}
        if size:
            attributes["size"] = size
        if color:
            attributes["color"] = color

    inventory = await store_client.check_inventory(product_id=int(product["id"]), attributes=attributes)
    is_in_stock = bool(inventory.get("in_stock"))
    qty = inventory.get("stock_quantity")

    actions: List[Dict[str, Any]] = [{
        "type": "show_availability",
        "payload": {"product": product, "inventory": inventory, "attributes": attributes or {}},
    }]

    if not is_in_stock:
        async with AsyncSessionLocal() as db:
            search_results = await hybrid_search(
                tenant_id=tenant_id,
                query=str(product.get("name") or ""),
                redis=None,
                db=db,
                store_client=store_client,
                in_stock_only=True,
                limit=4,
            )
        similar = []
        for r in search_results:
            similar.append({
                "id": r.platform_id,
                "name": r.name,
                "description": r.description,
                "price": r.price,
                "currency": r.currency,
                "image_url": r.image_url,
                "in_stock": r.in_stock,
                "category_slug": r.category_slug,
                "tags": r.tags,
                "permalink": r.permalink,
                "short_description": r.description[:200] if r.description else "",
            })
        if similar:
            actions.append({"type": "show_products", "payload": {"products": similar}})

    return with_actions_alias({
        "response_text": say(
            language, "availability",
            name=product.get("name", "Product"), size=size or "", qty=qty, in_stock=is_in_stock,
        ),
        "ui_actions": actions,
        "suggested_replies": ["Add to cart" if is_in_stock else "Show alternatives", "Show my cart"],
        "last_products": [product.get("id")],
    })


async def handle_compare(
    message: str,
    lower: str,
    last_products: List[Any],
    language: str,
    *,
    store_client: Any,
    tenant_id: str = "_dev",
) -> Optional[Dict[str, Any]]:
    terms = split_compare_terms(message)
    items: List[Dict[str, Any]] = []

    for term in terms:
        async with AsyncSessionLocal() as db:
            search_results = await hybrid_search(
                tenant_id=tenant_id,
                query=term,
                redis=None,
                db=db,
                store_client=store_client,
                in_stock_only=False,
                limit=1,
            )
        if search_results:
            r = search_results[0]
            items.append({
                "id": r.platform_id,
                "name": r.name,
                "price": r.price,
                "sale_price": "",
                "in_stock": r.in_stock,
                "image_url": r.image_url or "",
                "permalink": r.permalink or "",
            })

    if len(items) < 2 and len(last_products) >= 2:
        for pid in last_products[:3]:
            detail = await store_client.get_product_details(int(pid))
            items.append({
                "id": detail.get("id"),
                "name": detail.get("name"),
                "price": detail.get("price"),
                "sale_price": "",
                "in_stock": in_stock(detail),
                "image_url": detail.get("image_url") or "",
                "permalink": detail.get("permalink", ""),
            })

    deduped = []
    seen: set = set()
    for item in items:
        item_id = item.get("id")
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        deduped.append(item)

    if len(deduped) < 2:
        return with_actions_alias({
            "response_text": say(language, "need_two_compare"),
            "ui_actions": [],
            "suggested_replies": ["Show products"],
        })

    return with_actions_alias({
        "response_text": say(language, "comparison_ready"),
        "ui_actions": [{"type": "show_comparison", "payload": {"items": deduped[:3]}}],
        "suggested_replies": ["Add first one", "Check availability"],
        "last_products": [item.get("id") for item in deduped[:3]],
    })


async def handle_order_tracking(
    message: str,
    lower: str,
    state: Dict[str, Any],
    language: str,
    *,
    store_client: Any,
) -> Optional[Dict[str, Any]]:
    email = extract_email(lower) or state.get("customer_email")
    if not email:
        return with_actions_alias({
            "response_text": say(language, "ask_order_email"),
            "ui_actions": [],
            "suggested_replies": [],
        })

    orders = await store_client.get_orders(customer_email=email, limit=5)
    if not orders:
        return with_actions_alias({
            "response_text": say(language, "order_not_found"),
            "ui_actions": [],
            "suggested_replies": ["Show products"],
            "customer_email": email,
        })

    latest = orders[0]
    order_no = latest.get("order_number") or latest.get("order_id") or "-"
    status = latest.get("status", "processing")
    return with_actions_alias({
        "response_text": say(language, "order_status", order_no=order_no, status=status),
        "ui_actions": [{"type": "show_orders", "payload": {"orders": orders}}],
        "suggested_replies": ["Show my cart", "Show products"],
        "customer_email": email,
    })


async def handle_add_to_cart(
    message: str,
    lower: str,
    session_id: str,
    active_recommendations: List[Any],
    language: str,
    page_context: Optional[Dict[str, Any]] = None,
    *,
    store_client: Any,
    tenant_id: str = "",
    session_service: Any = None,
) -> Optional[Dict[str, Any]]:
    product = await _resolve_product_for_add(
        message=message,
        lower=lower,
        session_id=session_id,
        active_recommendations=active_recommendations,
        page_context=page_context or {},
        store_client=store_client,
        tenant_id=tenant_id,
        session_service=session_service,
    )
    if not product or not product.get("id"):
        return with_actions_alias({
            "response_text": say(language, "ask_add_which"),
            "ui_actions": [],
            "suggested_replies": ["Show products"],
        })

    variant_id = product.get("variant_id") or 0

    try:
        detail = await store_client.get_product_details(int(product["id"]))
    except Exception:
        detail = {}
    if not variant_id:
        variant_id = _first_available_variant_id(detail)

    if detail and detail.get("variants") and len(detail["variants"]) > 1:
        options = detail.get("options", [])
        all_values = []
        for o in options:
            vals = o.get("values", []) if isinstance(o, dict) else []
            all_values.extend(v.lower() for v in vals)
        has_size = any(o.get("name", "").lower() in ("size", "waist", "inseam") for o in options)
        has_color = any(o.get("name", "").lower() in ("color", "colour", "finish") for o in options)
        mentioned = False
        for val in all_values:
            if val in lower:
                mentioned = True
                break
        if not mentioned:
            if has_size and re.search(r"\b(small|medium|large|x[sl]?|s\b|m\b|l\b|xl|xxl)\b", lower):
                mentioned = True
            if not mentioned and has_color and re.search(r"\b(red|blue|black|white|green|pink|purple|gray|grey|gold|silver|navy|brown|beige)\b", lower):
                mentioned = True
        if not mentioned:
            options_text = ", ".join(
                f"{o.get('name', 'Option')}: {' / '.join(o.get('values', []))}"
                for o in options
            )
            return with_actions_alias({
                "response_text": say(language, "ask_variation", name=product.get("name", "product"), options=options_text),
                "ui_actions": [],
                "suggested_replies": ["Show me the options", "Add the first variant"],
            })

    return with_actions_alias({
        "response_text": say(language, "added_to_cart", name=product.get("name", "item"), qty="1"),
        "ui_actions": [{
            "type": "add_to_cart",
            "payload": {
                "product_id": int(product["id"]),
                "variant_id": int(variant_id),
                "quantity": 1,
                "permalink": product.get("permalink", ""),
                "handle": product.get("handle", ""),
            },
        }],
        "suggested_replies": ["Add another item", "View cart", "Proceed to checkout"],
        "last_products": [product.get("id")],
    })


async def handle_pdp_auto_tour(
    page_context: Dict[str, Any],
    *,
    store_client: Any,
    session_service: Any,
) -> Optional[Dict[str, Any]]:
    """
    Automatically explain on-screen products when the shopper lands on a PDP.
    Returns an AgentResponse with scroll actions and product description.
    """
    # Only trigger on product pages with a valid product_id
    if not page_context or page_context.get("page_type") != "product":
        return None
    product_id = page_context.get("product_id")
    if not product_id:
        return None
    
    try:
        product_id = int(product_id)
    except (TypeError, ValueError):
        return None
    
    # Fetch product details
    try:
        detail = await store_client.get_product_details(product_id)
    except Exception as e:
        logger.warning("PDP auto-tour: failed to fetch product %s: %s", product_id, e)
        return None
    
    if not detail or not detail.get("id"):
        return None
    
    name = detail.get("name", "Product")
    description = detail.get("description") or detail.get("short_description") or ""
    # Clean HTML tags for speech
    clean_desc = re.sub(r'<[^>]+>', '', description).strip()
    # Take the first sentence only — concise product summary
    sentences = re.split(r'(?<=[.!?])\s+', clean_desc)
    snippet = (sentences[0] if sentences else '').strip()
    if not snippet:
        snippet = f"The {name} is a great choice."

    # Best-effort: surface the default/active Size so the follow-up question can
    # say "add size 9 to your cart" instead of a generic prompt.
    size_hint = ""
    try:
        _variants = detail.get("variants") or []
        _active_variant = None
        _target_vid = page_context.get("variant_id")
        for _v in _variants:
            if isinstance(_v, dict) and _target_vid and int(_v.get("id") or 0) == int(_target_vid):
                _active_variant = _v
                break
        if _active_variant is None and isinstance(_variants, list) and _variants:
            _active_variant = _variants[0]
        if isinstance(_active_variant, dict):
            for _o in (_active_variant.get("options") or []):
                if isinstance(_o, dict) and str(_o.get("Name") or _o.get("name") or "").strip().lower() == "size":
                    _val = _o.get("Value") or _o.get("value") or ""
                    if _val:
                        size_hint = f" size {_val}"
                        break
    except Exception:
        size_hint = ""

    if size_hint:
        response_text = f"I see you're looking at the {name}. {snippet} Would you like me to add{size_hint} to your cart, or take you to checkout?"
    else:
        response_text = f"I see you're looking at the {name}. {snippet} Would you like me to add it to your cart, or take you to checkout?"
    
    # UI actions: scroll to description, then show variant picker, then scroll to form
    ui_actions = [
        {"type": "scroll_to", "payload": {"selector": ".product-description, [data-product-description], .product__description"}},
        {"type": "show_variant_picker", "payload": {"product_id": product_id}},
        {"type": "scroll_to", "payload": {"selector": ".product-form, .product__form, [data-product-form], form[action*='cart/add']"}},
    ]
    
    suggested_replies = ["Tell me more", "Add to cart", "Buy now"]
    
    return with_actions_alias({
        "response_text": response_text,
        "ui_actions": ui_actions,
        "suggested_replies": suggested_replies,
    })


def _current_search_query(page_context: Optional[Dict[str, Any]]) -> str:
    """Extract the live search term from the page the customer is on.

    The widget sends `url` in page_context; on a search page that is e.g.
    `https://store/search?q=red+sneakers` (Shopify) or `/?s=shoes` (Woo).
    Used to resolve "first/second item" against the listing the customer is
    actually looking at, instead of a stale session recommendation cache.
    """
    url = str((page_context or {}).get("url") or "").strip()
    if not url:
        return ""
    try:
        from urllib.parse import urlparse, parse_qs, unquote
        parsed = urlparse(url)
        q = (parse_qs(parsed.query).get("q") or [""])[0]
        if not q:
            q = (parse_qs(parsed.query).get("s") or [""])[0]
        if not q and parsed.path:
            m = re.search(r"/search/(.+)$", parsed.path)
            if m:
                q = m.group(1)
        return unquote(q.strip())
    except Exception:
        return ""


async def _resolve_product_for_add(
    message: str,
    lower: str,
    session_id: str,
    active_recommendations: List[Any],
    page_context: Optional[Dict[str, Any]] = None,
    *,
    store_client: Any,
    tenant_id: str = "",
    session_service: Any = None,
) -> Optional[Dict[str, Any]]:
    def _get_pid(p: Any) -> Optional[int]:
        pid = p.get("id") if isinstance(p, dict) else p
        try:
            return int(pid) if pid else None
        except (TypeError, ValueError):
            return None

    page_context = page_context or {}

    # 0. PDP Add-to-Cart — if on a product page, use page context first.
    if page_context.get("product_id"):
        try:
            detail = await store_client.get_product_details(int(page_context["product_id"]))
            if detail and detail.get("id"):
                vid = page_context.get("variant_id") or _first_available_variant_id(detail)
                return {
                    "id": detail.get("id"),
                    "name": detail.get("name", "Product"),
                    "permalink": detail.get("permalink", ""),
                    "variant_id": int(vid) if vid else 0,
                }
        except Exception as e:
            logger.error("Failed fetching PDP product pid %s: %s", page_context["product_id"], e)

    # 1. Resolve ordinal reference ("first" through "fifth") → active_recommendations or last_products
    _has_nav_only = bool(re.search(r"\b(show|open|view|look|display|see|navigate)\b", lower)) and not bool(
        re.search(r"\b(add|buy|purchase|cart|checkout)\b", lower)
    )
    target_index = None
    if not _has_nav_only and re.search(r"\b(first|1st|number one|no 1|no\. 1)\b", lower):
        target_index = 0
    elif not _has_nav_only and re.search(r"\b(second|2nd|number two|no 2|no\. 2)\b", lower):
        target_index = 1
    elif not _has_nav_only and re.search(r"\b(third|3rd|number three|no 3|no\. 3)\b", lower):
        target_index = 2
    elif not _has_nav_only and re.search(r"\b(fourth|4th|number four|no 4|no\. 4)\b", lower):
        target_index = 3
    elif not _has_nav_only and re.search(r"\b(fifth|5th|number five|no 5|no\. 5)\b", lower):
        target_index = 4

    if target_index is not None:
        # Resolve the ordinal against the products the customer ACTUALLY saw on
        # screen. The widget sends the displayed order in page_context["last_products"]
        # (it reorders the grid so the top recommendations are the first cards and
        # glows them one at a time). The session active_recommendations hold the
        # same recommended list. A fresh storefront search re-run is only a LAST
        # resort — its relevance order can differ from what was highlighted, which
        # made "take the first one" select a different (wrong) product.
        last_prods = page_context.get("last_products") or []
        if len(last_prods) > target_index:
            try:
                last_pid = last_prods[target_index]
                if isinstance(last_pid, dict):
                    last_pid = last_pid.get("id")
                if last_pid:
                    detail = await store_client.get_product_details(int(last_pid))
                    if detail and detail.get("id"):
                        return {
                            "id": detail.get("id"),
                            "name": detail.get("name", "Product"),
                            "permalink": detail.get("permalink", ""),
                            "variant_id": _first_available_variant_id(detail),
                        }
            except Exception as e:
                logger.error("Failed fetching last_product ordinal %s: %s", target_index, e)
        if active_recommendations and len(active_recommendations) > target_index:
            rec = active_recommendations[target_index]
            if isinstance(rec, dict) and rec.get("id"):
                return rec
        _search_q = _current_search_query(page_context)
        if _search_q:
            try:
                matches = await store_client.search_products(query=_search_q, in_stock_only=False, limit=12)
                if len(matches) > target_index:
                    best = matches[target_index]
                    if best and best.get("id"):
                        return best
            except Exception as e:
                logger.error("Failed resolving ordinal %d from current search page '%s': %s",
                             target_index, _search_q, e)
        logger.warning("Ordinal %d requested but no matching product in active_recommendations (%d) or last_products (%d) — returning None",
                       target_index, len(active_recommendations or []), len(last_prods))
        return None

    # 2. Resolve anaphoric reference ("add this", "get it", "add to cart")
    if re.search(r"\b(this|that|it|product|shoe|item)\b", lower):
        current_pid = page_context.get("product_id")
        if current_pid:
            try:
                detail = await store_client.get_product_details(int(current_pid))
                if detail and detail.get("id"):
                    return {
                        "id": detail.get("id"),
                        "name": detail.get("name", "Product"),
                        "permalink": detail.get("permalink", ""),
                        "variant_id": _first_available_variant_id(detail),
                    }
            except Exception as e:
                logger.error("Failed fetching context product for pid %s: %s", current_pid, e)

        if active_recommendations and len(active_recommendations) > 0:
            first_rec = active_recommendations[0]
            if isinstance(first_rec, dict) and first_rec.get("id"):
                return first_rec

        # 2a. Try SessionFacts last_product_id as fallback for "that/this/it"
        if session_service is not None and tenant_id and session_id:
            try:
                from ..memory.facts import get_session_facts_service
                facts = await get_session_facts_service().get(tenant_id, session_id)
                if facts:
                    last_pid = facts.get("last_product_id")
                    if last_pid:
                        detail = await store_client.get_product_details(int(last_pid))
                        if detail and detail.get("id"):
                            logger.info("Resolved anaphoric '%s' via SessionFacts last_product_id=%s", lower[:30], last_pid)
                            return {
                                "id": detail.get("id"),
                                "name": detail.get("name", "Product"),
                                "permalink": detail.get("permalink", ""),
                                "variant_id": _first_available_variant_id(detail),
                            }
            except Exception as e:
                logger.warning("SessionFacts fallback failed for anaphoric reference: %s", e)

    # 3. Explicit product_id in text
    product_id_match = re.search(r"product\s*id\s*(\d+)", lower)
    if product_id_match:
        pid = int(product_id_match.group(1))
        detail = await store_client.get_product_details(pid)
        if detail.get("id"):
            return {"id": detail.get("id"), "name": detail.get("name", "Product"), "permalink": detail.get("permalink", "")}

    # 4. Try search — if query resolves to a product, use it
    query = extract_add_query(message)
    if query:
        matches = await store_client.search_products(query=query, in_stock_only=False, limit=6)
        if matches:
            best = pick_best_product_match(query, matches)
            if best and best.get("id"):
                return best

    # 5. SessionFacts fallback — last known product from any previous turn
    if session_service is not None and tenant_id and session_id:
        try:
            from ..memory.facts import get_session_facts_service
            facts = await get_session_facts_service().get(tenant_id, session_id)
            if facts and facts.get("last_product_id"):
                pid = int(facts["last_product_id"])
                detail = await store_client.get_product_details(pid)
                if detail and detail.get("id"):
                    logger.info("SessionFacts fallback last_product_id=%s for query='%s'", pid, message[:60])
                    return {
                        "id": detail.get("id"),
                        "name": detail.get("name", "Product"),
                        "permalink": detail.get("permalink", ""),
                        "variant_id": _first_available_variant_id(detail),
                    }
        except Exception as e:
            logger.debug("SessionFacts fallback failed: %s", e)

    # 6. Fallback to first recommendation
    if active_recommendations:
        pid = _get_pid(active_recommendations[0])
        if pid:
            detail = await store_client.get_product_details(pid)
            if detail.get("id"):
                return {"id": detail.get("id"), "name": detail.get("name", "Product"), "permalink": detail.get("permalink", "")}

    return None
