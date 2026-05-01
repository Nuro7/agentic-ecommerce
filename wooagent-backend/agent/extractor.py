# AUDIT:
# 1. What is this file supposed to do? Takes the raw list of actions and builds declarative structures for the frontend.
# 2. What is it actually doing wrong? Fails to pass products into response objects due to incorrect payload construction/field references.
# 3. What exact lines are causing the failure? The generic mapping function not conforming to the expected frontend contract.

import logging

logger = logging.getLogger(__name__)

def extract_ui_actions(actions_taken: list) -> list:
    """
    Convert raw tool results into structured UI actions for the frontend.
    This is what makes products, cart, orders actually RENDER in the widget.
    """
    ui_actions = []
    
    for action in actions_taken:
        tool = action.get("tool", "")
        result = action.get("result", {})
        
        if not result.get("success", True):
            continue  # Skip failed tool calls
        
        if tool == "search_products":
            products = result.get("products", [])
            
            # CRITICAL FIX: Ensure image_url is in every product
            enriched_products = []
            for p in products:
                enriched = dict(p)
                
                if not enriched.get('image_url'):
                    images = enriched.get('images', [])
                    if images and isinstance(images, list):
                        enriched['image_url'] = images[0].get('src', '')
                    else:
                        enriched['image_url'] = ''
                
                enriched_products.append(enriched)
            
            if enriched_products:
                ui_actions.append({
                    "type": "show_products",
                    "payload": {
                        "products": enriched_products,
                        "total_found": result.get("total_found", len(enriched_products)),
                        "query": result.get("query", "")
                    }
                })
        
        elif tool == "get_product_details":
            if result.get("id"):
                ui_actions.append({
                    "type": "show_product_detail",
                    "payload": {"product": result}
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
                        "message": result.get("message", "")
                    }
                })
        
        elif tool == "get_cart":
            ui_actions.append({
                "type": "show_cart",
                "payload": {"cart": result}
            })
        
        elif tool == "get_orders":
            orders = result.get("orders", [])
            if orders:
                ui_actions.append({
                    "type": "show_orders",
                    "payload": {"orders": orders}
                })
        
        elif tool == "apply_coupon":
            if result.get("valid"):
                ui_actions.append({
                    "type": "coupon_applied",
                    "payload": {
                        "code": result.get("coupon_code"),
                        "discount": result.get("discount_display")
                    }
                })

        elif tool == "redirect_to_checkout":
            if result.get("success"):
                ui_actions.append({
                    "type": "redirect",
                    "payload": {
                        "url": result.get("checkout_url")
                    }
                })
    
    return ui_actions
