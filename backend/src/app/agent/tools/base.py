"""Tool registry — defines tools exposed to the LLM and dispatches calls."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from ...integrations.base.commerce import BaseStoreClient


@dataclass
class ToolExecution:
    result: Dict[str, Any]
    action: Dict[str, Any]


def get_tool_definitions() -> list:
    """Lightweight definitions for optional LLM tool-calling mode."""
    return [
        {"name": "search_products"},
        {"name": "get_product_details"},
        {"name": "check_inventory"},
        {"name": "get_cart"},
        {"name": "add_to_cart"},
        {"name": "remove_from_cart"},
        {"name": "get_orders"},
        {"name": "apply_coupon"},
        {"name": "get_categories"},
        {"name": "get_store_info"},
        {"name": "compare_products"},
        {"name": "update_cart_quantity"},
        {"name": "find_variants"},
        {"name": "get_best_coupon"},
        {"name": "submit_review"},
    ]


async def execute_tool(
    tool_name: str,
    tool_args: Dict[str, Any],
    session_id: str,
    store_client: BaseStoreClient,
) -> ToolExecution:
    args = dict(tool_args or {})

    if tool_name == "search_products":
        products = await store_client.search_products(
            query=str(args.get("query", "")),
            category_slug=args.get("category_slug") or args.get("category"),
            min_price=_to_float(args.get("min_price")),
            max_price=_to_float(args.get("max_price")),
            in_stock_only=_to_bool(args.get("in_stock_only"), default=True),
            on_sale=_to_optional_bool(args.get("on_sale")),
            limit=_to_int(args.get("limit"), default=6),
        )
        result = {
            "success": True,
            "query": str(args.get("query", "")),
            "products": products,
            "total_found": len(products),
        }
        action = {"type": "show_products", "payload": {"products": products}}
        return ToolExecution(result=result, action=action)

    if tool_name == "get_product_details":
        product_id = _to_int(args.get("product_id"), default=0)
        product = await store_client.get_product_details(product_id)
        return ToolExecution(
            result={"success": True, "product": product},
            action={"type": "show_product_detail", "payload": {"product": product}},
        )

    if tool_name == "check_inventory":
        inventory = await store_client.check_inventory(
            product_id=_to_int(args.get("product_id"), default=0),
            variation_id=_to_optional_int(args.get("variation_id")),
            attributes=args.get("attributes"),
        )
        return ToolExecution(
            result={"success": True, "inventory": inventory},
            action={"type": "show_availability", "payload": {"inventory": inventory}},
        )

    if tool_name == "get_cart":
        cart = await store_client.get_cart(session_id=session_id)
        return ToolExecution(
            result={"success": True, "cart": cart},
            action={"type": "show_cart", "payload": {"cart": cart}},
        )

    if tool_name == "add_to_cart":
        product_id = _to_int(args.get("product_id"), default=0)
        variation_id = _to_optional_int(args.get("variation_id"))
        inv = None
        if not variation_id and args.get("attributes"):
            inv = await store_client.check_inventory(
                product_id=product_id,
                attributes=args.get("attributes"),
            )
            variation_id = _to_optional_int(inv.get("variation_id"))

        variation_data = args.get("attributes")
        if not variation_data and inv and inv.get("attributes"):
            variation_data = {item["name"]: item["option"] for item in inv["attributes"]}

        cart_result = await store_client.add_to_cart(
            session_id=session_id,
            product_id=product_id,
            variation_id=variation_id or 0,
            quantity=_to_int(args.get("quantity"), default=1),
            variation=variation_data,
        )

        return ToolExecution(
            result={"success": True, "cart": cart_result},
            action={
                "type": "add_to_cart",
                "payload": {
                    "product_id": product_id,
                    "variation_id": variation_id or 0,
                    "quantity": _to_int(args.get("quantity"), default=1),
                    "variation": variation_data,
                },
            },
        )

    if tool_name == "remove_from_cart":
        remove_result = await store_client.remove_from_cart(
            session_id=session_id,
            cart_item_key=args.get("cart_item_key"),
            product_id=_to_optional_int(args.get("product_id")),
        )
        return ToolExecution(
            result={"success": True, "cart": remove_result},
            action={"type": "show_cart", "payload": {}},
        )

    if tool_name == "get_orders":
        orders = await store_client.get_orders(
            customer_email=str(args.get("customer_email", "")).strip().lower(),
            limit=_to_int(args.get("limit"), default=5),
        )
        action_payload = {"order": orders[0]} if orders else {}
        return ToolExecution(
            result={"success": True, "orders": orders},
            action={"type": "show_order", "payload": action_payload},
        )

    if tool_name == "apply_coupon":
        coupon = await store_client.apply_coupon(
            session_id=session_id,
            coupon_code=str(args.get("coupon_code", "")).strip(),
        )
        return ToolExecution(
            result={"success": True, "coupon": coupon},
            action={"type": "apply_coupon", "payload": {"code": coupon.get("code", "")}},
        )

    if tool_name == "get_categories":
        categories = await store_client.get_categories()
        return ToolExecution(
            result={"success": True, "categories": categories},
            action={"type": "show_categories", "payload": {"categories": categories}},
        )

    if tool_name == "get_store_info":
        info = await store_client.get_store_policies()
        return ToolExecution(
            result={"success": True, "store_info": info},
            action={"type": "show_store_info", "payload": {"store_info": info}},
        )

    if tool_name == "compare_products":
        product_a = str(args.get("product_a", "")).strip()
        product_b = str(args.get("product_b", "")).strip()
        compared = []
        for query in [product_a, product_b]:
            if not query:
                continue
            rows = await store_client.search_products(
                query=query,
                in_stock_only=False,
                limit=1,
            )
            if rows:
                row = rows[0]
                compared.append({
                    "id": row.get("id"),
                    "name": row.get("name"),
                    "price": row.get("price"),
                    "sale_price": row.get("sale_price"),
                    "in_stock": str(row.get("stock_status", "")).lower() == "instock",
                })

        return ToolExecution(
            result={"success": True, "comparison": compared},
            action={"type": "show_comparison", "payload": {"items": compared}},
        )

    if tool_name == "update_cart_quantity":
        pid = _to_int(args.get("product_id"), default=0)
        qty = _to_int(args.get("quantity"), default=0)
        result = await store_client.update_cart_quantity(
            session_id=session_id, product_id=pid, quantity=qty
        )
        return ToolExecution(
            result={"success": True, "update_cart_quantity": result},
            action={"type": "cart_updated", "payload": {"cart": result.get("updated_cart", {})}},
        )

    if tool_name == "find_variants":
        pid = _to_int(args.get("product_id"), default=0)
        result = await store_client.get_product_variations(product_id=pid)
        return ToolExecution(
            result={"success": True, "find_variants": result},
            action={"type": "show_variants", "payload": result},
        )

    if tool_name == "get_best_coupon":
        total = _to_float(args.get("cart_total")) or 0.0
        result = await store_client.get_best_coupon(cart_total=total)
        return ToolExecution(
            result={"success": True, "get_best_coupon": result},
            action={"type": "show_best_coupon", "payload": result},
        )

    if tool_name == "submit_review":
        pid = _to_int(args.get("product_id"), default=0)
        rating = _to_int(args.get("rating"), default=5)
        text = str(args.get("review_text") or "")
        name = str(args.get("reviewer_name") or "")
        result = await store_client.submit_review(
            product_id=pid, rating=rating, review=text, name=name
        )
        return ToolExecution(
            result={"success": True, "submit_review": result},
            action={"type": "review_submitted", "payload": result},
        )

    return ToolExecution(
        result={"success": False, "error": f"Unknown tool: {tool_name}"},
        action={"type": "noop", "payload": {}},
    )


def _to_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _to_optional_int(value: Any) -> Optional[int]:
    try:
        if value in (None, "", 0, "0"):
            return None
        return int(value)
    except Exception:
        return None


def _to_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _to_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _to_optional_bool(value: Any) -> Optional[bool]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return None
