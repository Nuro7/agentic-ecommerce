"""Recommendation engine — fetches promoted/dead-stock products for the agent to recommend.

Integrates with the brain: before building the system prompt, the brain calls
get_promoted_products() which returns active offers with product details, injected
into the prompt so the agent naturally recommends them.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .repository import OfferRepository

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
