import logging

logger = logging.getLogger(__name__)


def extract_ui_actions(actions_taken: list) -> list:
    """Convert raw tool results into structured UI actions for the frontend widget."""
    ui_actions = []

    for action in actions_taken:
        tool = action.get("tool", "")
        result = action.get("result", {})

        if not result.get("success", True):
            continue

        if tool == "search_products":
            products = result.get("products", [])
            enriched = []
            for p in products:
                ep = dict(p)
                if not ep.get('image_url'):
                    images = ep.get('images', [])
                    if images and isinstance(images, list):
                        ep['image_url'] = images[0].get('src', '')
                    else:
                        ep['image_url'] = ''
                enriched.append(ep)
            if enriched:
                ui_actions.append({
                    "type": "show_products",
                    "payload": {
                        "products": enriched,
                        "total_found": result.get("total_found", len(enriched)),
                        "query": result.get("query", ""),
                    },
                })

        elif tool == "get_product_details":
            if result.get("id"):
                ui_actions.append({
                    "type": "show_product_detail",
                    "payload": {"product": result},
                })

        elif tool in ("add_to_cart", "remove_from_cart", "update_cart_quantity"):
            updated_cart = result.get("updated_cart")
            if updated_cart:
                ui_actions.append({
                    "type": "cart_updated",
                    "payload": {
                        "cart": updated_cart,
                        "item_count": updated_cart.get("item_count", 0),
                        "total": updated_cart.get("total", "₹0"),
                        "message": result.get("message", ""),
                    },
                })

        elif tool == "get_cart":
            ui_actions.append({"type": "show_cart", "payload": {"cart": result}})

        elif tool == "get_orders":
            orders = result.get("orders", [])
            if orders:
                ui_actions.append({"type": "show_orders", "payload": {"orders": orders}})

        elif tool == "apply_coupon":
            if result.get("valid"):
                ui_actions.append({
                    "type": "coupon_applied",
                    "payload": {
                        "code": result.get("coupon_code"),
                        "discount": result.get("discount_display"),
                    },
                })

        elif tool == "redirect_to_checkout":
            if result.get("success"):
                ui_actions.append({
                    "type": "redirect",
                    "payload": {"url": result.get("checkout_url")},
                })

    return ui_actions
