"""Fast deterministic intent handlers — no LLM, ~0ms responses."""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

from .canned import say
from .text_utils import (
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
)

logger = logging.getLogger(__name__)


# Backwards-compatible local alias — the matcher now lives in text_utils so the
# brain's fast-intent gate (core.py) can share it.
def _wants_cart_navigation(lower: str) -> bool:
    return has_cart_nav_intent(lower)


async def safe_get_cart(
    tenant_id: str,
    session_id: str,
    *,
    store_client: Any,
    session_service: Any,
) -> Dict[str, Any]:
    try:
        cart = await store_client.get_live_cart(session_id=session_id)
        await session_service.save_cart(tenant_id, session_id, cart)
        return cart
    except Exception as e:
        logger.warning("Live cart fetch failed, using cache: %s", e)
        cart = await session_service.get_cart(tenant_id, session_id)
        if cart and not cart.get("is_empty", True):
            return cart
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
) -> Optional[Dict[str, Any]]:
    text = str(message or "")
    lower = text.lower()
    store_name = str((store_context or {}).get("store_name") or "").strip()

    # Per-tenant store config (tenant DB column → env-var fallback). A tenant
    # with NULL columns behaves exactly as before (env text for all tenants).
    from ...modules.tenants.service import get_store_config_for_tenant
    cfg = await get_store_config_for_tenant(tenant_id)

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

    if _wants_cart_navigation(lower):
        cart = await safe_get_cart(tenant_id, session_id, store_client=store_client, session_service=session_service)
        cart_url = str((store_context or {}).get("cart_url") or "/cart")
        # Render inline AND navigate the storefront to the real cart page, so
        # "go to cart" actually moves the page (the #7 indication-driven UX).
        return with_actions_alias({
            "response_text": say(language, "cart_opened"),
            "ui_actions": [
                {"type": "show_cart", "payload": {"cart": normalize_cart_payload(cart)}},
                {"type": "redirect", "payload": {"url": cart_url}},
            ],
            "suggested_replies": ["Checkout now", "Show products"],
        })

    if has_cart_view_intent(lower):
        cart = await safe_get_cart(tenant_id, session_id, store_client=store_client, session_service=session_service)
        return with_actions_alias({
            "response_text": say(language, "cart_opened"),
            "ui_actions": [{"type": "show_cart", "payload": {"cart": normalize_cart_payload(cart)}}],
            "suggested_replies": ["Checkout now", "Show products"],
        })

    if has_remove_intent(lower):
        cart = await safe_get_cart(tenant_id, session_id, store_client=store_client, session_service=session_service)
        items = cart.get("items") if isinstance(cart.get("items"), list) else []
        if not items:
            return with_actions_alias({
                "response_text": say(language, "cart_empty"),
                "ui_actions": [{"type": "show_cart", "payload": {"cart": normalize_cart_payload(cart)}}],
                "suggested_replies": ["Show products"],
            })
        target = items[-1]
        try:
            await store_client.remove_from_cart(session_id=session_id, cart_item_key=target.get("cart_item_key"))
        except Exception:
            pass
        cart_after = await safe_get_cart(tenant_id, session_id, store_client=store_client, session_service=session_service)
        return with_actions_alias({
            "response_text": say(language, "removed_from_cart", name=target.get("name", "item")),
            "ui_actions": [{"type": "show_cart", "payload": {"cart": normalize_cart_payload(cart_after)}}],
            "suggested_replies": ["Checkout now", "Show products"],
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
    products = await store_client.search_products(
        query=query, min_price=min_price, max_price=max_price,
        in_stock_only=in_stock_only, limit=limit,
    )
    if not products:
        products = await store_client.search_products(
            query="", min_price=min_price, max_price=max_price,
            in_stock_only=False, limit=24,
        )
    if not products:
        return with_actions_alias({
            "response_text": say(language, "no_products"),
            "ui_actions": [],
            "suggested_replies": ["Show products", "Show my cart"],
        })

    best = pick_best_product_match(lower, products)
    if best and best in products:
        products.remove(best)
        products.insert(0, best)
    products = products[:6]

    name = products[0].get("name", "")
    price = products[0].get("price", "")
    price_text = f", ₹{price}" if price else ""
    response = f"{name}{price_text} — want me to show the size and color options?"

    return with_actions_alias({
        "response_text": response,
        "ui_actions": [{"type": "show_products", "payload": {"products": products}}],
        "suggested_replies": ["Show options", "Add to cart", "Show my cart"],
        "last_products": [p.get("id") for p in products if p.get("id")],
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

    products = await store_client.search_products(query=query, in_stock_only=False, limit=4)
    if not products:
        return None

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


async def handle_availability(
    message: str,
    lower: str,
    last_products: List[Any],
    language: str,
    *,
    store_client: Any,
) -> Optional[Dict[str, Any]]:
    size, color = extract_size_color(lower)
    query = normalize_availability_query(message)

    product: Optional[Dict[str, Any]] = None
    if query:
        rows = await store_client.search_products(query=query, in_stock_only=False, limit=6)
        if rows:
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
        similar = await store_client.search_products(query=str(product.get("name") or ""), in_stock_only=True, limit=4)
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
) -> Optional[Dict[str, Any]]:
    terms = split_compare_terms(message)
    items: List[Dict[str, Any]] = []

    for term in terms:
        rows = await store_client.search_products(query=term, in_stock_only=False, limit=1)
        if rows:
            row = rows[0]
            items.append({
                "id": row.get("id"),
                "name": row.get("name"),
                "price": row.get("price"),
                "sale_price": row.get("sale_price"),
                "in_stock": in_stock(row),
                "image_url": row.get("image_url") or (row.get("images", [{}])[0].get("src") if row.get("images") else ""),
                "permalink": row.get("permalink", ""),
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
                "image_url": detail.get("image_url") or (detail.get("images", [{}])[0].get("src") if detail.get("images") else ""),
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
    last_products: List[Any],
    language: str,
    *,
    store_client: Any,
) -> Optional[Dict[str, Any]]:
    qty = extract_quantity(lower)
    size, color = extract_size_color(lower)

    product = await _resolve_product_for_add(message, lower, last_products, store_client=store_client)
    if not product or not product.get("id"):
        return with_actions_alias({
            "response_text": say(language, "ask_add_which"),
            "ui_actions": [],
            "suggested_replies": ["Show products"],
        })

    variation_id = 0
    variation_data: Dict[str, Any] = {}
    attributes: Optional[Dict[str, str]] = None
    if size or color:
        attributes = {}
        if size:
            attributes["size"] = size
        if color:
            attributes["color"] = color

    if attributes:
        inventory = await store_client.check_inventory(product_id=int(product["id"]), attributes=attributes)
        variation_id = int(inventory.get("variation_id") or 0)
        if hasattr(store_client, "_attributes_to_variation_map"):
            variation_data = store_client._attributes_to_variation_map(inventory.get("attributes", []))
        if not inventory.get("in_stock"):
            alternatives = await store_client.search_products(query=str(product.get("name") or ""), in_stock_only=True, limit=4)
            actions = [{"type": "show_availability", "payload": {"product": product, "inventory": inventory, "attributes": attributes or {}}}]
            if alternatives:
                actions.append({"type": "show_products", "payload": {"products": alternatives}})
            return with_actions_alias({
                "response_text": say(language, "out_of_stock", name=product.get("name", "Product"), size=size or ""),
                "ui_actions": actions,
                "suggested_replies": ["Show alternatives"],
                "last_products": [p.get("id") for p in alternatives if p.get("id")],
            })
        if qty <= 0 or qty == 1:
            if not re.search(r'\b(\d+)\s*(piece|pcs|qty|quantity|units?|nos?|number)?\b', lower):
                product_name = product.get("name", "Product")
                size_label = f" size {size}" if size else ""
                color_label = f" {color}" if color else ""
                return with_actions_alias({
                    "response_text": f"Great choice!{color_label}{size_label} — How many {product_name} would you like to add?",
                    "ui_actions": [],
                    "suggested_replies": ["1", "2", "3"],
                    "last_products": [product.get("id")],
                    "_pending_add": {
                        "product_id": int(product["id"]),
                        "variation_id": variation_id,
                        "variation": variation_data,
                    },
                })

    if not attributes:
        detail = await store_client.get_product_details(product_id=int(product["id"]))
        variations = await store_client.find_variants(product_id=int(product["id"]))
        if variations and variations.get("variations"):
            return with_actions_alias({
                "response_text": f"Please select the specific options for {product.get('name', 'this product')} to add it to your cart.",
                "ui_actions": [{"type": "show_variants", "payload": {"product": detail, "variations": variations.get("variations", [])}}],
                "suggested_replies": ["Show details", "Cancel"],
                "last_products": [product.get("id")],
            })
        if qty <= 0 or qty == 1:
            if not re.search(r'\b(\d+)\s*(piece|pcs|qty|quantity|units?|nos?|number)?\b', lower):
                return with_actions_alias({
                    "response_text": f"How many {product.get('name', 'items')} would you like to add to your cart?",
                    "ui_actions": [],
                    "suggested_replies": ["1", "2", "3"],
                    "last_products": [product.get("id")],
                })

    final_qty = max(1, qty)
    return with_actions_alias({
        "response_text": say(language, "added_to_cart", name=product.get("name", "Product"), qty=final_qty),
        "ui_actions": [{
            "type": "add_to_cart",
            "payload": {
                "product_id": int(product["id"]),
                "variation_id": variation_id,
                "variation": variation_data,
                "quantity": final_qty,
            },
        }],
        "suggested_replies": ["Add another item", "View cart", "Proceed to checkout"],
        "last_products": [product.get("id")],
    })


async def _resolve_product_for_add(
    message: str,
    lower: str,
    last_products: List[Any],
    *,
    store_client: Any,
) -> Optional[Dict[str, Any]]:
    def _get_pid(p: Any) -> Optional[int]:
        pid = p.get("id") if isinstance(p, dict) else p
        try:
            return int(pid) if pid else None
        except (TypeError, ValueError):
            return None

    if any(token in lower for token in ["add it", "add this", "add first", "yes add", "add one"]):
        if last_products:
            pid = _get_pid(last_products[0])
            if pid:
                detail = await store_client.get_product_details(pid)
                return {"id": detail.get("id"), "name": detail.get("name", "Product")}

    product_id_match = re.search(r"product\s*id\s*(\d+)", lower)
    if product_id_match:
        pid = int(product_id_match.group(1))
        detail = await store_client.get_product_details(pid)
        if detail.get("id"):
            return {"id": detail.get("id"), "name": detail.get("name", "Product")}

    query = extract_add_query(message)
    if query:
        matches = await store_client.search_products(query=query, in_stock_only=False, limit=6)
        if matches:
            return pick_best_product_match(query, matches)

    if last_products:
        pid = _get_pid(last_products[0])
        if pid:
            detail = await store_client.get_product_details(pid)
            return {"id": detail.get("id"), "name": detail.get("name", "Product")}

    return None
