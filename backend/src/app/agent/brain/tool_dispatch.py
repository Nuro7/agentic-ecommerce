"""Tool execution dispatcher and OpenAI-compatible tool schema."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from .text_utils import safe_int, safe_optional_int, safe_float, normalize_discovery_query
from ..retrieval.search import hybrid_search
from ..retrieval.normalizer import normalize as _normalize_for_size
from ...core.database import AsyncSessionLocal

logger = logging.getLogger(__name__)


async def _fetch_compare_item(store_client: Any, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Fetch + assemble one comparison row (search-or-detail + variations). Returns None if not found."""
    from .text_utils import in_stock as _in_stock

    row = None
    if item.get("id"):
        row = await store_client.get_product_details(int(item["id"]))
    elif item.get("name"):
        rows = await store_client.search_products(query=item["name"], in_stock_only=False, limit=1)
        row = rows[0] if rows else None
    if not (row and row.get("id")):
        return None
    details = (
        await store_client.get_product_details(int(row.get("id") or row.get("product_id") or 0))
        if not row.get("variations")
        else row
    )
    return {
        "id": details.get("id") or row.get("id"),
        "name": details.get("name") or row.get("name"),
        "price": details.get("price") or row.get("price"),
        "sale_price": details.get("sale_price") or row.get("sale_price"),
        "in_stock": _in_stock(details or row),
        "image_url": (details or row).get("image_url") or "",
        "permalink": (details or row).get("permalink", ""),
        "short_description": (details or row).get("short_description", ""),
        "rating": (details or row).get("average_rating") or (details or row).get("rating_count"),
    }


async def execute_tool_call(
    tool_name: str,
    tool_args: Dict[str, Any],
    session_id: str,
    cart_context: Optional[Dict[str, Any]],
    *,
    tenant_id: str,
    store_client: Any,
    session_service: Any,
    conversation_history: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Any], Optional[str]]:
    """Dispatch a single tool call and return (result, ui_actions, product_ids, customer_email)."""
    logger.info("[FLOW] tool_dispatch ENTER tool=%s args=%s session=%s", tool_name, json.dumps(tool_args, default=str)[:120], session_id)
    actions: List[Dict[str, Any]] = []
    product_ids: List[Any] = []
    customer_email: Optional[str] = None

    if tool_name == "search_products":
        raw_query = str(tool_args.get("query", "")).strip()
        brand = str(tool_args.get("brand", "") or "").strip()
        if brand and brand.lower() not in raw_query.lower():
            raw_query = f"{brand} {raw_query}".strip()
        query = normalize_discovery_query(raw_query)
        default_limit = 5 if not query else 4
        requested_limit = safe_int(tool_args.get("limit"), default_limit)
        limit = max(1, min(requested_limit, 8))
        in_stock_only = bool(tool_args.get("in_stock_only", False))
        sort_by = str(tool_args.get("sort_by") or "").strip() or None
        sort_order = str(tool_args.get("sort_order") or "").strip() or None
        
        # Use hybrid_search (L3: BM25 + vector + RRF) instead of live store API
        # This provides stemming, fuzzy matching, and semantic search
        # normalize_discovery_query strips size/price/colour from the text, so
        # re-extract them from the RAW query (or the explicit tool params) and
        # pass them as structured filters.
        nq_raw = _normalize_for_size(raw_query)
        size = str(tool_args.get("size") or "").strip() or None
        if not size:
            size = nq_raw.size
        color = str(tool_args.get("color") or "").strip() or None
        if not color:
            color = nq_raw.color
        min_price = safe_float(tool_args.get("min_price"))
        if min_price is None:
            min_price = nq_raw.min_price
        max_price = safe_float(tool_args.get("max_price"))
        if max_price is None:
            max_price = nq_raw.max_price
        async with AsyncSessionLocal() as db:
            search_results = await hybrid_search(
                tenant_id=tenant_id,
                query=query,
                redis=None,  # Redis passed from orchestrator if needed
                db=db,
                store_client=store_client,
                min_price=min_price,
                max_price=max_price,
                in_stock_only=in_stock_only,
                size=size,
                color=color,
                limit=limit,
                sort_by=sort_by,
                sort_order=sort_order,
            )
        
        # Convert SearchResult objects to dict format expected by rest of code
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
        
        brand_filtered: List[Dict[str, Any]] = []
        if brand and products:
            bl = brand.lower()
            brand_filtered = [
                p for p in products
                if bl in str(p.get("name") or "").lower()
                or bl in str(p.get("short_description") or p.get("description") or "").lower()
            ]
            brand_found = len(brand_filtered) > 0
        else:
            brand_found = True

        actions.append({"type": "show_products", "payload": {"products": products}})
        product_ids = [p.get("id") for p in products if p.get("id")]
        compact = [
            {"id": p.get("id"), "name": p.get("name"), "price": p.get("price"),
             "in_stock": p.get("in_stock"), "image_url": p.get("image_url"),
             "handle": p.get("handle"), "permalink": p.get("permalink"),
             "variant_id": p.get("variant_id")}
            for p in products
        ]

        # Highlight the first in-stock product (bound by product id so the widget
        # glows the exact card for this product, not a raw array index).
        first_in_stock = next((p for p in products if p.get("in_stock")), products[0] if products else None)
        if first_in_stock:
            idx = products.index(first_in_stock)
            actions.append({
                "type": "highlight_card",
                "payload": {"target_index": idx, "product_id": first_in_stock.get("id")},
            })
        result: Dict[str, Any] = {"products": compact, "count": len(products)}
        if brand:
            result["brand_searched"] = brand
            result["brand_found"] = brand_found
            if not brand_found:
                result["note"] = f"Brand '{brand}' not found in catalog. Showing similar alternatives — tell the customer we don't carry that brand but suggest the best alternative."
        return result, actions, product_ids, None

    if tool_name == "get_product_details":
        raw_pid = tool_args.get("product_id")
        product_id = safe_int(raw_pid, 0)
        if not product_id and raw_pid and isinstance(raw_pid, str):
            logger.info("get_product_details: resolving name '%s' to ID via search", raw_pid)
            matches = await store_client.search_products(query=raw_pid, in_stock_only=False, limit=1)
            if matches:
                product_id = int(matches[0].get("id") or 0)
                logger.info("Resolved product name '%s' → id=%d", raw_pid, product_id)
        product = await store_client.get_product_details(product_id)
        if product.get("id"):
            product_ids.append(product.get("id"))
        actions.append({"type": "show_product_detail", "payload": {"product": product}})
        return {"product": product}, actions, product_ids, None

    if tool_name == "check_inventory":
        product_id = safe_int(tool_args.get("product_id"), 0)
        inventory = await store_client.check_inventory(
            product_id=product_id,
            variation_id=safe_optional_int(tool_args.get("variation_id")),
            attributes=tool_args.get("attributes"),
        )
        details = await store_client.get_product_details(product_id)
        product_name = details.get("name", "That product")
        actions.append({
            "type": "show_availability",
            "payload": {
                "product": {
                    "id": details.get("id"),
                    "name": product_name,
                    "price": details.get("price"),
                    "image_url": details.get("image_url", ""),
                    "stock_status": details.get("stock_status"),
                },
                "inventory": inventory,
                "attributes": tool_args.get("attributes", {}),
            },
        })
        if inventory.get("variant_not_found"):
            hint = (
                f"The exact variant is not available for '{product_name}'. "
                f"Call find_variants(product_id={product_id}) to show the customer available options."
            )
        elif inventory.get("in_stock"):
            qty = inventory.get("stock_quantity")
            qty_str = f" — {qty} units in stock" if isinstance(qty, int) and qty > 0 else ""
            hint = (
                f"'{product_name}' IS IN STOCK{qty_str}. "
                "Tell the customer it's available and ask if they'd like to add it to cart."
            )
        else:
            hint = (
                f"'{product_name}' is OUT OF STOCK. "
                "Apologize briefly and offer to show similar in-stock alternatives."
            )
        return {"inventory": inventory, "response_hint": hint}, actions, [product_id], None

    if tool_name == "get_cart":
        # Fetch LIVE cart from storefront API instead of using stale cart_context
        live_cart = await store_client.get_cart(session_id=session_id)
        actions.append({"type": "show_cart", "payload": {"cart": _normalize_cart(live_cart)}})
        return {"cart": live_cart}, actions, [], None

    if tool_name == "add_to_cart":
        product_id = safe_int(tool_args.get("product_id"), 0)
        logger.info(
            "[TOOL] add_to_cart called session=%s product_id=%s variation_id=%s quantity=%s attributes=%s",
            session_id, tool_args.get("product_id"), tool_args.get("variation_id"),
            tool_args.get("quantity"), tool_args.get("attributes"),
        )
        if not product_id:
            return {"error": "A valid product ID is required to add to cart. Please search for the product first."}, actions, [], None
        variation_id = safe_int(tool_args.get("variation_id"), 0)
        quantity = max(1, min(safe_int(tool_args.get("quantity"), 1), 20))
        if not variation_id and tool_args.get("attributes"):
            try:
                inv = await store_client.check_inventory(
                    product_id=product_id,
                    attributes=tool_args.get("attributes"),
                )
                variation_id = safe_int(inv.get("variation_id"), 0)
            except Exception:
                pass

        # Look up product to get handle + permalink for the widget's AJAX call
        product_handle = ""
        product_permalink = ""
        product_detail = {}
        try:
            product_detail = await store_client.get_product_details(product_id)
            product_handle = str(product_detail.get("handle") or "")
            product_permalink = str(product_detail.get("permalink") or "")
        except Exception:
            pass

        # Shopify /cart/add.js needs a real variant ID — never the product ID.
        # If the LLM didn't pass one, pick the first purchasable variant.
        if not variation_id:
            for v in (product_detail.get("variations") or product_detail.get("variations_summary") or []):
                if isinstance(v, dict) and v.get("id"):
                    status = str(v.get("stock_status") or "").lower()
                    if status != "outofstock":
                        variation_id = safe_int(v.get("id"), 0)
                        break
            if not variation_id:
                for v in (product_detail.get("variations") or product_detail.get("variations_summary") or []):
                    if isinstance(v, dict) and v.get("id"):
                        variation_id = safe_int(v.get("id"), 0)
                        break

        actions.append({
            "type": "add_to_cart",
            "payload": {
                "product_id": product_id,
                "variant_id": variation_id or 0,
                "quantity": quantity,
                "handle": product_handle,
                "permalink": product_permalink,
            },
        })
        return {"add_to_cart": "dispatched", "message": "Adding to cart…"}, actions, [product_id], None

    if tool_name == "remove_from_cart":
        cart_item_key = str(tool_args.get("cart_item_key") or "").strip()
        if not cart_item_key:
            return {"error": "A valid cart_item_key is required to remove from cart."}, actions, [], None
        actions.append({
            "type": "remove_from_cart",
            "payload": {"cart_item_key": cart_item_key},
        })
        return {"remove_from_cart": "dispatched", "message": "Removing from cart…"}, actions, [], None

    if tool_name == "get_orders":
        from ..guardrails import is_pii_placeholder
        from ...services.ticketing import _collect_customer_context

        email = str(tool_args.get("customer_email") or "").strip().lower()
        # A redaction placeholder ([email]) is NOT a usable email — fall back to
        # the session meta (server-side real values captured before redaction).
        if not email or is_pii_placeholder(email):
            ctx = await _collect_customer_context(session_service, tenant_id, session_id, "")
            email = str(ctx.get("email") or "").strip().lower()
        if not email:
            return (
                {
                    "orders": [],
                    "error": "No customer email available. Ask the customer for their email address so we can look up their orders.",
                },
                actions, [], None,
            )
        orders = await store_client.get_orders(customer_email=email, limit=5)
        if orders:
            actions.append({"type": "show_orders", "payload": {"orders": orders}})
        customer_email = email
        return {"orders": orders}, actions, [], customer_email

    if tool_name == "apply_coupon":
        code = str(tool_args.get("coupon_code") or "").strip()
        result = await store_client.apply_coupon(session_id=session_id, coupon_code=code)
        actions.append({"type": "coupon_applied", "payload": {"code": code, "discount": result.get("message", "Applied")}})
        return {"coupon": result}, actions, [], None

    if tool_name == "get_categories":
        try:
            categories = await store_client.get_categories()
        except Exception as cat_err:
            logger.warning("get_categories failed (%s), falling back to product search", cat_err)
            categories = []
        if categories:
            cat_names = [str(c.get("name", "")) for c in categories if c.get("name")]
            return {"categories": categories, "category_names": cat_names}, actions, [], None
        products = await store_client.search_products(query="", in_stock_only=False, limit=12)
        products = [p for p in (products or []) if isinstance(p, dict)]
        product_ids = [p.get("id") for p in products if p.get("id")]
        product_names = [p.get("name", "") for p in products if p.get("name")][:8]
        if products:
            actions.append({"type": "show_products", "payload": {"products": [products[0]]}})
        return {
            "categories": [],
            "note": "Category listing unavailable. The first product card is already shown. Recommend ONE product from this list by name, then ask if the customer wants to see more options.",
            "available_products": product_names,
            "count": len(products),
        }, actions, product_ids, None

    if tool_name == "update_cart_quantity":
        pid = safe_int(tool_args.get("product_id"), 0)
        qty = safe_int(tool_args.get("quantity"), 0)
        result = await store_client.update_cart_quantity(session_id=session_id, product_id=pid, quantity=qty)
        actions.append({"type": "cart_updated", "payload": result})
        return {"update_cart_quantity": result}, actions, [], None

    if tool_name == "find_variants":
        pid = safe_int(tool_args.get("product_id"), 0)
        result = await store_client.find_variants(product_id=pid)
        detail = await store_client.get_product_details(pid)
        variations = result.get("variations") or []
        if not variations and detail.get("variations_summary"):
            variations = detail["variations_summary"]
        payload = {"product": detail, "variations": variations}
        actions.append({"type": "show_variants", "payload": payload})
        var_count = len(variations)
        tool_result_msg = (
            f"Variant selector shown to user ({var_count} options for '{detail.get('name', '')}')."
            " IMPORTANT: Do NOT call add_to_cart now. Tell the user to select size/color/quantity"
            " from the options shown above, then tap Add to Cart."
        ) if var_count > 0 else (
            f"No variants found for '{detail.get('name', '')}'. Ask the user which option they need."
        )
        return {
            "find_variants": {
                "product_name": detail.get("name", ""),
                "variations_count": var_count,
                "message": tool_result_msg,
            }
        }, actions, [pid], None

    if tool_name == "get_best_coupon":
        result = await store_client.get_best_coupon()
        if result.get("code"):
            discount_type = result.get("discount_type") or result.get("type")
            actions.append({"type": "show_best_coupon", "payload": result})
            return {
                "coupon_available": True,
                "code": result["code"],
                "amount": result.get("amount"),
                "discount_type": discount_type,
                "display": result.get("display", ""),
            }, actions, [], None
        return {"coupon_available": False, "message": "No active coupons in this store right now."}, actions, [], None

    if tool_name == "submit_review":
        pid = safe_int(tool_args.get("product_id"), 0)
        rating = safe_int(tool_args.get("rating"), 5)
        text = str(tool_args.get("review") or "")
        name = str(tool_args.get("name") or "")
        result = await store_client.submit_review(product_id=pid, rating=rating, review=text, name=name)
        actions.append({"type": "review_submitted", "payload": result})
        return {"submit_review": result}, actions, [pid], None

    if tool_name == "apply_conversational_discount":
        campaign_id = str(tool_args.get("campaign_id") or "")
        discount_percentage = float(tool_args.get("discount_percentage", 0))
        return await _apply_conversational_discount(
            store_client=store_client,
            session_id=session_id,
            campaign_id=campaign_id,
            discount_percentage=discount_percentage,
            actions=actions,
        )

    if tool_name == "request_human_support":
        customer_email = str(tool_args.get("customer_email", "")).strip().lower()
        return await _request_human_support(
            store_client=store_client,
            customer_email=customer_email,
            conversation_history=conversation_history,
            actions=actions,
            tenant_id=tenant_id,
            session_id=session_id,
            session_service=session_service,
        )

    if tool_name == "create_support_ticket":
        return await _create_support_ticket(
            store_client=store_client,
            tenant_id=tenant_id,
            session_id=session_id,
            session_service=session_service,
            conversation_history=conversation_history,
            tool_args=tool_args,
            actions=actions,
        )

    if tool_name == "get_store_info":
        info = await store_client.get_store_policies()
        return {"store_info": info}, actions, [], None

    if tool_name == "compare_products":
        raw_ids = tool_args.get("product_ids") or []
        raw_names = [tool_args.get("product_a"), tool_args.get("product_b")]
        to_fetch: List[Any] = []
        if raw_ids and isinstance(raw_ids, list):
            to_fetch = [{"id": int(x)} for x in raw_ids if x]
        else:
            to_fetch = [{"name": str(n).strip()} for n in raw_names if n]
        to_fetch = to_fetch[:3]
        # Each item is an independent API chain — fetch them concurrently instead of
        # awaiting one-at-a-time (was the slow part of a multi-product compare).
        results = await asyncio.gather(
            *[_fetch_compare_item(store_client, item) for item in to_fetch],
            return_exceptions=True,
        )
        compare_items: List[Dict[str, Any]] = []
        for item, res in zip(to_fetch, results):
            if isinstance(res, BaseException):
                logger.warning("compare_products fetch failed for %s: %s", item, res)
                continue
            if res:
                compare_items.append(res)
        if len(compare_items) >= 2:
            actions.append({"type": "show_comparison", "payload": {"items": compare_items}})
        return {"comparison": compare_items, "count": len(compare_items)}, actions, [i.get("id") for i in compare_items if i.get("id")], None

    if tool_name == "get_reviews":
        product_id = safe_int(tool_args.get("product_id"), 0)
        if not product_id:
            return {"error": "product_id required"}, actions, [], None
        data = await store_client.get_reviews(product_id)
        actions.append({"type": "show_reviews", "payload": {
            "product_id": product_id,
            "reviews": data.get("reviews", []),
            "average_rating": data.get("average_rating", 0),
            "count": data.get("count", 0),
        }})
        return data, actions, [], None

    if tool_name == "add_multiple_to_cart":
        items_to_add = tool_args.get("items") or []
        results = []
        for item in items_to_add[:5]:
            pid = safe_int(item.get("product_id"), 0)
            if not pid:
                continue
            qty = max(1, safe_int(item.get("quantity"), 1))
            actions.append({
                "type": "add_to_cart",
                "payload": {
                    "product_id": pid,
                    "variation_id": safe_int(item.get("variation_id"), 0),
                    "variation": item.get("attributes") or {},
                    "quantity": qty,
                },
            })
            results.append({"product_id": pid, "queued": True})
        return {"results": results, "note": "client_side_action"}, actions, [], None

    if tool_name == "trigger_store_event":
        event_name = str(tool_args.get("event", "")).strip()
        product_id = safe_int(tool_args.get("product_id"), 0)
        detail = {}
        if product_id:
            detail["product_id"] = product_id
        options = tool_args.get("options")
        if options:
            detail["options"] = options
        selector = tool_args.get("selector")
        if selector:
            detail["selector"] = selector
        actions.append({
            "type": "store_event",
            "payload": {
                "event": event_name,
                "detail": detail,
            },
        })
        return {"store_event": event_name}, actions, [product_id] if product_id else [], None

    return {"ignored_tool": tool_name}, actions, product_ids, customer_email


async def _apply_conversational_discount(
    *,
    store_client: Any,
    session_id: str,
    campaign_id: str,
    discount_percentage: float,
    actions: List[Dict[str, Any]],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Any], Optional[str]]:
    from ...services.promotions import generate_and_apply_discount
    campaign = {
        "campaign_id": campaign_id,
        "discount_percentage": discount_percentage,
    }
    result = await generate_and_apply_discount(
        store_client=store_client,
        session_id=session_id,
        campaign=campaign,
    )
    if result.get("success"):
        actions.append({
            "type": "coupon_applied",
            "payload": {
                "code": result.get("code", ""),
                "discount": result.get("message", "Discount applied"),
            },
        })
    return result, actions, [], None


async def _request_human_support(
    *,
    store_client: Any,
    customer_email: str,
    conversation_history: Optional[List[Dict[str, Any]]],
    actions: List[Dict[str, Any]],
    tenant_id: str,
    session_id: str,
    session_service: Any,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Any], Optional[str]]:
    from ...services.ticketing import create_support_ticket
    from ..guardrails import is_pii_placeholder

    customer_email = str(tool_args.get("customer_email", "")).strip().lower()
    if is_pii_placeholder(customer_email):
        customer_email = ""
    ticket_result = await create_support_ticket(
        tenant_id=tenant_id,
        session_id=session_id,
        conversation_history=conversation_history or [],
        store_client=store_client,
        session_service=session_service,
        customer_email=customer_email,
        source="llm",
    )
    if ticket_result.get("status") == "success":
        actions.append({
            "type": "show_ticket",
            "payload": {
                "ticket_id": ticket_result["ticket_id"],
                "ticket_number": ticket_result.get("ticket_number"),
                "priority": ticket_result.get("priority", "medium"),
                "heat": ticket_result.get("heat"),
                "message": ticket_result.get("message", ""),
            },
        })
    return ticket_result, actions, [], customer_email or None


async def _create_support_ticket(
    *,
    store_client: Any,
    tenant_id: str,
    session_id: str,
    session_service: Any,
    conversation_history: Optional[List[Dict[str, Any]]],
    tool_args: Dict[str, Any],
    actions: List[Dict[str, Any]],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Any], Optional[str]]:
    from ...services.ticketing import create_support_ticket
    from ..guardrails import is_pii_placeholder

    customer_email = str(tool_args.get("customer_email", "") or "").strip().lower()
    customer_phone = str(tool_args.get("customer_phone", "") or "").strip()
    customer_name = str(tool_args.get("customer_name", "") or "").strip()
    trigger_message = str(tool_args.get("issue_description", "") or "").strip()
    # Redaction placeholders ([email]/[phone]) are not real contact data — treat
    # them as empty so create_support_ticket fills from session meta instead.
    if is_pii_placeholder(customer_email):
        customer_email = ""
    if is_pii_placeholder(customer_phone):
        customer_phone = ""

    ticket_result = await create_support_ticket(
        tenant_id=tenant_id,
        session_id=session_id,
        conversation_history=conversation_history or [],
        store_client=store_client,
        session_service=session_service,
        customer_email=customer_email,
        customer_phone=customer_phone,
        customer_name=customer_name,
        trigger_message=trigger_message,
        source="llm",
    )
    if ticket_result.get("status") == "success":
        actions.append({
            "type": "show_ticket",
            "payload": {
                "ticket_id": ticket_result["ticket_id"],
                "ticket_number": ticket_result.get("ticket_number"),
                "priority": ticket_result.get("priority", "medium"),
                "heat": ticket_result.get("heat"),
                "message": ticket_result.get("message", ""),
            },
        })
    return ticket_result, actions, [], customer_email or None


def _normalize_cart(cart: Dict[str, Any]) -> Dict[str, Any]:
    item_count = int(cart.get("item_count") or cart.get("count") or 0)
    return {
        "is_empty": item_count == 0,
        "item_count": item_count,
        "total": str(cart.get("total") or "₹0"),
        "items": cart.get("items") or [],
    }


def tool_schema() -> List[Dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "search_products",
                "description": "Search for products by name, brand, category, price, and attributes. Use the 'brand' parameter when customer asks for a specific brand (e.g. Nike, Adidas). If brand returns no results, re-search without brand to find similar alternatives.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Product name or keyword to search for"},
                        "brand": {"type": "string", "description": "Brand name filter (e.g. 'Nike', 'Adidas'). Include brand name in query as well for best results."},
                        "category": {"type": "string"},
                        "min_price": {"type": "number"},
                        "max_price": {"type": "number"},
                        "in_stock_only": {"type": "boolean"},
                        "limit": {"type": "integer"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_product_details",
                "description": (
                    "Get full details for a product including all variants, sizes, colors, and images. "
                    "IMPORTANT: You MUST call search_products first to get the numeric product_id. "
                    "Pass product_id as an INTEGER number (e.g. 123), NOT a product name string."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "product_id": {
                            "type": "integer",
                            "description": "The numeric product ID (integer) obtained from search_products result. NOT a product name.",
                        },
                    },
                    "required": ["product_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "check_inventory",
                "description": "Check if a specific product variant (color + size combination) is in stock. Pass attributes as a key-value dict, e.g. {\"color\": \"red\", \"size\": \"M\"}. Returns in_stock, stock_quantity, and the matched variation_id. If variant_not_found is true in the response, that exact combo does not exist — call find_variants to see what IS available.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "product_id": {"type": "integer"},
                        "variation_id": {"type": "integer", "description": "Optional: specific variation ID if already known"},
                        "attributes": {"type": "object", "description": "Key-value pair of variation attributes, e.g. {\"color\": \"red\", \"size\": \"M\"}"},
                    },
                    "required": ["product_id"],
                },
            },
        },
        {"type": "function", "function": {"name": "get_cart", "description": "Get customer cart", "parameters": {"type": "object", "properties": {}}}},
        {
            "type": "function",
            "function": {
                "name": "add_to_cart",
                "description": "Add a product to cart with optional variant and quantity.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "product_id": {"type": "integer"},
                        "variation_id": {"type": "integer"},
                        "quantity": {"type": "integer"},
                        "attributes": {"type": "object", "description": "Key-value pair of variation attributes selected by customer"},
                    },
                    "required": ["product_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "remove_from_cart",
                "description": "Remove cart item by cart_item_key.",
                "parameters": {"type": "object", "properties": {"cart_item_key": {"type": "string"}}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_orders",
                "description": "Get recent orders by customer email.",
                "parameters": {
                    "type": "object",
                    "properties": {"customer_email": {"type": "string"}},
                    "required": ["customer_email"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "apply_coupon",
                "description": "Apply discount coupon to cart.",
                "parameters": {
                    "type": "object",
                    "properties": {"coupon_code": {"type": "string"}},
                    "required": ["coupon_code"],
                },
            },
        },
        {"type": "function", "function": {"name": "get_categories", "description": "Get product categories.", "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {"name": "get_store_info", "description": "Get store policies and capabilities.", "parameters": {"type": "object", "properties": {}}}},
        {
            "type": "function",
            "function": {
                "name": "compare_products",
                "description": (
                    "Compare 2-3 products side by side. PREFERRED: pass product_ids as a list of integers "
                    "from prior search results. Fallback: pass product_a and product_b as search strings."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "product_ids": {"type": "array", "items": {"type": "integer"}, "description": "List of 2-3 numeric product IDs to compare (preferred over product_a/product_b)"},
                        "product_a": {"type": "string", "description": "Product name to search (fallback)"},
                        "product_b": {"type": "string", "description": "Product name to search (fallback)"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_reviews",
                "description": "Get customer reviews and ratings for a product. Use when customer asks about reviews, ratings, feedback, or wants to know if a product is good. After fetching, summarise naturally: mention the average rating, what customers consistently praise, and any common complaints — like a friend summarising word-of-mouth. Never read out individual reviews verbatim.",
                "parameters": {
                    "type": "object",
                    "properties": {"product_id": {"type": "integer", "description": "Numeric product ID"}},
                    "required": ["product_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "add_multiple_to_cart",
                "description": "Add multiple products to cart in one go. Use when customer wants to buy several items at once.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "description": "List of products to add",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "product_id": {"type": "integer"},
                                    "quantity": {"type": "integer"},
                                    "attributes": {"type": "object"},
                                },
                                "required": ["product_id"],
                            },
                        },
                    },
                    "required": ["items"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "update_cart_quantity",
                "description": "Update the quantity of an item in the cart.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "product_id": {"type": "integer"},
                        "quantity": {"type": "integer"},
                    },
                    "required": ["product_id", "quantity"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_variants",
                "description": "Get all variations for a variable product with stock per variant. Critical for size/color selection.",
                "parameters": {
                    "type": "object",
                    "properties": {"product_id": {"type": "integer"}},
                    "required": ["product_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_best_coupon",
                "description": "Find the best available coupon for the customer (discount amount/description). no arguments needed.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "apply_conversational_discount",
                "description": "Generate and apply a single-use promotional discount code to the customer's cart. Call this when the customer agrees to an offer you pitched during a campaign promotion.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "campaign_id": {
                            "type": "string",
                            "description": "The campaign_id from the promotion directive (e.g. 'dead_stock_clearance').",
                        },
                        "discount_percentage": {
                            "type": "number",
                            "description": "The discount percentage to apply (e.g. 15.0 for 15% off).",
                        },
                    },
                    "required": ["campaign_id", "discount_percentage"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "create_support_ticket",
                "description": (
                    "Create a support ticket for a human agent to review. Call this when: "
                    "(1) the customer explicitly asks to talk to a human / agent / manager, "
                    "(2) the customer reports a problem you cannot solve — refund or exchange "
                    "requests, damaged, broken, defective, wrong, or missing items, order or "
                    "delivery complaints, billing errors, or (3) a product/catalog query you "
                    "could not resolve. Persists the full chat transcript, auto-detects "
                    "priority, and notifies the store team. After calling, reassure the "
                    "customer using the message in the result."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "customer_email": {
                            "type": "string",
                            "description": "Customer's email if known from the session or conversation.",
                        },
                        "customer_phone": {
                            "type": "string",
                            "description": "Customer's phone number if known from the session or conversation.",
                        },
                        "customer_name": {
                            "type": "string",
                            "description": "Customer's name if known.",
                        },
                        "issue_description": {
                            "type": "string",
                            "description": "Short summary of the problem the customer reported, in the customer's own words where possible.",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "request_human_support",
                "description": "Escalate to a human support agent. Call this only when the customer explicitly demands to speak to a human, is highly frustrated, or asks for a manager. Creates a support ticket and notifies the team. (Newer callers should use create_support_ticket, which also handles refunds/damaged-order issues.)",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "customer_email": {
                            "type": "string",
                            "description": "Customer's email address for the support ticket.",
                        },
                    },
                    "required": ["customer_email"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "submit_review",
                "description": "Submit a product review.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "product_id": {"type": "integer"},
                        "rating": {"type": "integer", "description": "Rating out of 5"},
                        "review": {"type": "string"},
                        "name": {"type": "string"},
                    },
                    "required": ["product_id", "rating", "review", "name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "trigger_store_event",
                "description": "Trigger a store-specific UI action — open cart drawer, open product modal, select variant option, scroll to a section, or fire a custom store event. Use when the customer asks to 'open cart', 'show reviews', 'try this on', 'see size guide', or any store UI action.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "event": {
                            "type": "string",
                            "description": "Event name. Built-in: speako:open_cart_drawer, speako:open_product_modal, speako:select_variant, speako:scroll_to. Use speako:<custom_name> for merchant-wired events."
                        },
                        "product_id": {
                            "type": "integer",
                            "description": "Product ID when the event relates to a specific product."
                        },
                        "options": {
                            "type": "object",
                            "description": "Variant options for select_variant: {'Size': 'M', 'Color': 'Black'}"
                        },
                        "selector": {
                            "type": "string",
                            "description": "CSS selector for scroll_to events (e.g. '#shopify-product-reviews')."
                        },
                    },
                    "required": ["event"],
                },
            },
        },
    ]
