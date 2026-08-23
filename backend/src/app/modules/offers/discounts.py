"""Bound discount code minting for Shopify (combo/bulk offers)."""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from ...integrations.factory import _build_client
from ...modules.tenants.models import Tenant
from ...core.database import AsyncSessionLocal
from sqlalchemy import select

logger = logging.getLogger(__name__)


async def _has_write_discounts_scope(tenant: Tenant) -> bool:
    scope = getattr(tenant, "shopify_scope", "") or ""
    return "write_discounts" in scope


async def mint_bound_code_for_offer(tenant: Tenant, offer: Any) -> Optional[str]:
    platform = getattr(tenant, "platform", "shopify")
    if platform != "shopify":
        return None

    if not await _has_write_discounts_scope(tenant):
        logger.info("Tenant %s lacks write_discounts scope — skipping bound code mint", tenant.id)
        return None

    # Use _build_client (correct signature) instead of create_store_client
    client = _build_client(tenant)

    if not client or not client.has_credentials:
        return None

    try:
        target_pids: List[str] = []
        if offer.offer_kind == "combo" and offer.combo_items:
            target_pids = [str(item.get("platform_id")) for item in offer.combo_items if item.get("platform_id")]
        elif offer.offer_kind == "bulk" and offer.platform_id:
            target_pids = [str(offer.platform_id)]
        else:
            return None

        if not target_pids:
            return None

        if offer.offer_kind == "combo":
            async with AsyncSessionLocal() as db:
                from sqlalchemy import select
                from ...modules.products.models import ProductCache
                stmt = select(ProductCache.platform_id, ProductCache.price).where(
                    ProductCache.tenant_id == tenant.id,
                    ProductCache.platform_id.in_(target_pids),
                )
                result = await db.execute(stmt)
                prices = {pid: float(price or 0) for pid, price in result.all()}
            full_price = sum(prices.get(pid, 0) for pid in target_pids)
            combo_price = float(getattr(offer, "combo_price", 0) or 0)
            discount_amount = round(max(0.0, full_price - combo_price), 2)
            discount_type = "fixed_amount"
            value = discount_amount
        else:
            tiers = getattr(offer, "bulk_tiers", []) or []
            if not tiers:
                return None
            best_tier = max(tiers, key=lambda t: t.get("min_qty", 1))
            discount_pct = float(best_tier.get("discount_percent", 0) or 0)
            if discount_pct <= 0:
                return None
            discount_type = "percentage"
            value = discount_pct / 100.0

        code = f"SPEAKO-{uuid.uuid4().hex[:8].upper()}"

        # Use the existing client.create_discount_code wrapper (consolidated)
        # For combo: fixed-amount-off scoped to products via minimumRequirement
        # For bulk: percentage with quantity minimum
        if offer.offer_kind == "combo":
            # Use Admin GraphQL directly for product-scoped fixed amount
            # (create_discount_code only supports percentage)
            product_gids = [f"gid://shopify/Product/{pid}" for pid in target_pids]
            mutation = """
            mutation discountCodeBasicCreate($basicCodeDiscount: DiscountCodeBasicInput!) {
              discountCodeBasicCreate(basicCodeDiscount: $basicCodeDiscount) {
                codeDiscountNode {
                  codeDiscount {
                    ... on DiscountCodeBasic {
                      codes(first: 1) { edges { node { code } } }
                    }
                  }
                }
                userErrors { field message }
              }
            }
            """
            variables = {
                "basicCodeDiscount": {
                    "title": f"Speako {offer.offer_kind}: {offer.title}",
                    "code": code,
                    "startsAt": "2024-01-01T00:00:00Z",
                    "customerSelection": {"forAllCustomers": True},
                    "customerGets": {
                        "value": {
                            "discountAmount": {
                                "amount": str(value),
                                "appliesOnEachItem": False,
                            }
                        },
                        "items": {
                            "products": {"productsToAdd": product_gids}
                        },
                    },
                    "minimumRequirement": {
                        "quantity": {"greaterThanOrEqualToQuantity": str(len(target_pids))}
                    },
                    "usageLimit": 1,
                    "appliesOncePerCustomer": True,
                    "combinesWith": {
                        "orderDiscounts": False,
                        "productDiscounts": False,
                        "shippingDiscounts": True,
                    },
                }
            }
            data = await client._admin_graphql(mutation, variables)
            errors = data.get("discountCodeBasicCreate", {}).get("userErrors", [])
            if errors:
                logger.warning("Bound code mint failed for offer %s: %s", offer.id, errors)
                return None
        else:
            # Bulk: use existing create_discount_code wrapper (percentage)
            result = await client.create_discount_code(code, value, f"speako-bulk-{offer.id}")
            if not result.get("success"):
                logger.warning("Bulk bound code mint failed for offer %s: %s", offer.id, result.get("message"))
                return None

        logger.info("Minted bound code %s for offer %s (tenant %s)", code, offer.id, tenant.id)
        return code

    except Exception as exc:
        logger.warning("mint_bound_code_for_offer failed: %s", exc)
        return None
    finally:
        if client:
            try:
                await client.close()
            except Exception:
                pass


async def ensure_offer_has_bound_code(tenant_id: str, offer_id: str) -> None:
    async with AsyncSessionLocal() as db:
        from ...modules.tenants.repository import TenantRepository
        from ...modules.offers.repository import OfferRepository

        tenant = await TenantRepository(db).get_by_id(tenant_id)
        if not tenant:
            return

        offer = await OfferRepository(db).get_by_id(offer_id, tenant_id)
        if not offer:
            return

        if offer.offer_kind not in ("combo", "bulk"):
            return

        if getattr(offer, "discount_code", None):
            return

        code = await mint_bound_code_for_offer(tenant, offer)
        if code:
            offer.discount_code = code
            await db.commit()