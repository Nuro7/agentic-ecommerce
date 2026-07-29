import json
import logging
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def rerank_and_annotate_promotions(
    products: List[Dict[str, Any]],
    active_campaigns: List[Dict[str, Any]],
    w_promo_default: float = 1.5,
    w_margin_default: float = 1.3,
) -> List[Dict[str, Any]]:
    """
    Reranks search results based on active merchant campaigns and margin definitions.
    Injects campaign data into the product dictionary so the LLM knows what to pitch.

    S_final = S_base * w_promo * w_margin

    `products` should have a "relevance_score" key (default 1.0).
    `active_campaigns` is a list of campaign dicts from Shopify Shop Metafields.
    """
    scored: List[Dict[str, Any]] = []

    for prod in products:
        tags = prod.get("tags", "")
        if isinstance(tags, list):
            tag_list = [str(t).lower() for t in tags]
        elif isinstance(tags, str):
            tag_list = [t.strip().lower() for t in tags.split(",")]
        else:
            tag_list = []

        base_score = float(prod.get("relevance_score", 1.0))

        w_promo = 1.0
        active_campaign: Optional[Dict[str, Any]] = None

        for campaign in active_campaigns:
            target_tag = (campaign.get("target_tag") or "").strip().lower()
            if target_tag and target_tag in tag_list:
                w_promo = w_promo_default
                active_campaign = campaign
                break

        w_margin = float(prod.get("margin_weight", 1.0))
        if w_margin < 1.0:
            w_margin = w_margin_default

        final_score = base_score * w_promo * w_margin
        prod["_rerank_score"] = final_score

        if active_campaign:
            prod["is_promo_item"] = True
            prod["promo_badge"] = "🔥 Limited Clearance Offer"
            prod["campaign_id"] = active_campaign.get("campaign_id")
            prod["discount_percentage"] = active_campaign.get("discount_percentage")
            prod["pitch_hook"] = active_campaign.get("pitch_hook")
            prod["discount_type"] = active_campaign.get("discount_type", "percentage")

        scored.append(prod)

    scored.sort(key=lambda x: x.get("_rerank_score", 1.0), reverse=True)
    return scored


def parse_shop_metafield_campaigns(raw_json: Optional[str]) -> List[Dict[str, Any]]:
    """
    Parse the campaigns JSON from a Shopify Shop Metafield.
    Returns a list of campaign dicts, or empty list on failure.
    Expected metafield namespace: 'speako', key: 'campaigns'.
    """
    if not raw_json:
        return []
    try:
        data = json.loads(raw_json)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            campaigns = data.get("campaigns") or data.get("rules") or []
            return campaigns if isinstance(campaigns, list) else [data]
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("Failed to parse campaign metafield JSON: %s", exc)
    return []


async def generate_and_apply_discount(
    *,
    store_client: Any,
    session_id: str,
    campaign_id: str,
    discount_percentage: float,
    shopify_cart_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generates a single-use discount code via Shopify Admin API
    and applies it to the customer's active cart via Storefront API.

    Steps:
      1. Call discountCodeBasicCreate (Admin GraphQL)
      2. Call cartDiscountCodesUpdate (Storefront GraphQL)

    Returns dict with success status, code, and message.
    """
    if not store_client:
        return {"success": False, "message": "No store client available"}

    code = f"SPEAKO-{uuid.uuid4().hex[:8].upper()}"

    try:
        result = await store_client.create_discount_code(
            code=code,
            discount_percentage=discount_percentage,
            campaign_id=campaign_id,
        )
        if not result.get("success"):
            return result
    except Exception as exc:
        logger.warning("Failed to create discount code: %s", exc)
        return {"success": False, "message": f"Failed to create discount: {exc}"}

    cart_id = shopify_cart_id
    if not cart_id:
        try:
            cart_id = await store_client._get_cart_id(session_id)
        except Exception:
            pass

    if cart_id:
        try:
            await store_client.apply_discount_to_cart(
                cart_id=cart_id,
                discount_code=code,
            )
        except Exception as exc:
            logger.warning("Failed to apply discount to cart: %s", exc)
            return {
                "success": True,
                "code": code,
                "message": f"Discount {code} created but could not auto-apply to cart. Please enter it at checkout.",
                "auto_applied": False,
            }

    return {
        "success": True,
        "code": code,
        "message": f"Discount of {discount_percentage}% applied! Your code is {code}. It has been applied to your cart.",
        "auto_applied": bool(cart_id),
    }
