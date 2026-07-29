import uuid
import os
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CAMPAIGN_NAMESPACE = "speako"
CAMPAIGN_METAKEY = "campaigns"


async def load_active_campaigns(store_client: Any) -> List[Dict[str, Any]]:
    """Read active campaign rules from Shopify Shop Metafields.
    Returns a list of campaign dicts, or empty list on any error.
    """
    if not store_client:
        return []
    try:
        gql = """
        query GetShopCampaigns {
          shop {
            metafields(namespace: "%s", first: 10) {
              edges {
                node {
                  key
                  value
                }
              }
            }
          }
        }
        """ % CAMPAIGN_NAMESPACE
        data = await store_client._admin_graphql(gql)
        edges = (
            data.get("shop", {})
            .get("metafields", {})
            .get("edges", [])
        )
        for edge in edges:
            node = edge.get("node", {})
            if node.get("key") == CAMPAIGN_METAKEY:
                raw = node.get("value", "[]")
                campaigns = _safe_json_loads(raw, [])
                if isinstance(campaigns, list):
                    logger.info("Loaded %d campaign rules from shop metafields", len(campaigns))
                    return campaigns
        logger.debug("No speako.campaigns metafield found on shop")
        return []
    except Exception as exc:
        logger.warning("Failed to load campaigns from metafields: %s", exc)
        return []


def _safe_json_loads(raw: str, default: Any = None) -> Any:
    try:
        import json
        return json.loads(raw)
    except Exception:
        return default


def rerank_and_annotate_promotions(
    products: List[Dict[str, Any]],
    active_campaigns: List[Dict[str, Any]],
    w_margin_default: float = 1.3,
) -> List[Dict[str, Any]]:
    """Rerank products by S_final = S_base * w_promo * w_margin.
    Annotate items matching a campaign with promo flags.
    """
    scored: List[Dict[str, Any]] = []
    for prod in products:
        tags = prod.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        tags_lower = [str(t).lower() for t in tags]

        base_score = float(prod.get("relevance_score", 1.0))
        w_promo = 1.0
        active_campaign: Optional[Dict[str, Any]] = None

        for campaign in active_campaigns:
            target_tag = (campaign.get("target_tag") or "").lower()
            if target_tag and target_tag in tags_lower:
                w_promo = 1.5
                active_campaign = campaign
                break

        is_high_margin = bool(prod.get("is_high_margin", False))
        w_margin = w_margin_default if is_high_margin else 1.0

        final_score = base_score * w_promo * w_margin
        prod["_rerank_score"] = final_score

        if active_campaign:
            prod["is_promo_item"] = True
            prod["promo_badge"] = "\U0001f525 Limited Clearance Offer"
            prod["campaign_id"] = active_campaign.get("campaign_id")
            prod["discount_percentage"] = active_campaign.get("discount_percentage")
            prod["pitch_hook"] = active_campaign.get("pitch_hook")

        scored.append(prod)

    scored.sort(key=lambda x: float(x.get("_rerank_score", 1.0)), reverse=True)
    return scored


async def generate_and_apply_discount(
    session_id: str,
    store_client: Any,
    campaign: Dict[str, Any],
    customer_email: Optional[str] = None,
    cart_total: Optional[float] = None,
) -> Dict[str, Any]:
    """Generate a single-use discount code (Admin API) and apply it to the
    active cart (Storefront API). Returns the coupon code + updated cart.
    """
    try:
        code = f"SPEAKO-{uuid.uuid4().hex[:8].upper()}"
        discount_pct = float(campaign.get("discount_percentage", 10))

        gql = """
        mutation discountCodeBasicCreate($input: DiscountCodeBasicInput!) {
          discountCodeBasicCreate(input: $input) {
            codeDiscountNode {
              codeDiscount {
                ... on DiscountCodeBasic {
                  codes(first: 5) {
                    edges { node { code } }
                  }
                }
              }
            }
            userErrors { field message }
          }
        }
        """
        variables = {
            "input": {
                "code": code,
                "usageLimit": 1,
                "appliesOnOneTimePurchase": True,
                "customerSelection": {"all": True},
                "appliesOnSubscription": False,
                "combineWith": {
                    "orderDiscountApplications": True,
                    "productDiscounts": True,
                    "shippingDiscounts": True,
                },
                "startsAt": "2024-01-01T00:00:00Z",
                "endsAt": None,
                "minimumRequirement": {"quantity": {"greaterThanOrEqualTo": 1}},
                "value": {
                    "percentage": discount_pct / 100.0,
                },
            }
        }
        if customer_email:
            variables["input"]["customerSelection"] = {
                "customers": [{"email": customer_email}]
            }

        admin_data = await store_client._admin_graphql(gql, variables)
        errors = admin_data.get("discountCodeBasicCreate", {}).get("userErrors", [])
        if errors:
            logger.warning("Discount creation failed: %s", errors)
            return {"success": False, "message": f"Discount creation failed: {errors}"}

        logger.info("Generated discount code %s via Admin API", code)

        cart_id = await store_client._get_cart_id(session_id)
        if cart_id:
            sf_gql = """
            mutation CartDiscountCodesUpdate($cartId: ID!, $discountCodes: [String!]!) {
              cartDiscountCodesUpdate(cartId: $cartId, discountCodes: $discountCodes) {
                cart { id checkoutUrl
                  cost { totalAmount { amount currencyCode } }
                  lines(first: 50) { edges { node {
                    id quantity
                    merchandise { ... on ProductVariant { id title product { id title } image { url } } }
                    cost { amountPerQuantity { amount } subtotalAmount { amount } }
                  } } }
                }
                userErrors { field message }
              }
            }
            """
            sf_data = await store_client._storefront(sf_gql, {
                "cartId": cart_id,
                "discountCodes": [code],
            })
            sf_errors = sf_data.get("cartDiscountCodesUpdate", {}).get("userErrors", [])
            if sf_errors:
                logger.warning("Cart discount apply failed: %s", sf_errors)
                return {
                    "success": True,
                    "code": code,
                    "message": f"Code {code} generated but could not auto-apply. Please enter at checkout.",
                }

            cart_node = sf_data.get("cartDiscountCodesUpdate", {}).get("cart")
            if cart_node:
                cart_snapshot = store_client._normalize_cart(cart_node)
                return {
                    "success": True,
                    "code": code,
                    "discount_percent": discount_pct,
                    "cart": cart_snapshot,
                    "message": f"Discount of {discount_pct:.0f}% applied!",
                }

        return {
            "success": True,
            "code": code,
            "discount_percent": discount_pct,
            "message": f"Discount code {code} generated. Apply at checkout.",
        }

    except Exception as exc:
        logger.warning("generate_and_apply_discount failed: %s", exc)
        return {"success": False, "message": f"Failed to create discount: {exc}"}
