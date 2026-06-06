import json

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from .service import BillingService
from ..tenants.repository import TenantRepository
from ...core.database import get_db
from ...core.exceptions import NotFoundError

# Credits allowed on the implicit free tier (tenants with no subscription row).
# All-time total — no monthly reset.
_FREE_TIER_LIMIT = 50


async def enforce_conversation_quota(
    tenant_id: str,
    db: AsyncSession,
    *,
    is_voice: bool = False,
) -> None:
    """
    Raise HTTP 402 if the tenant has exceeded their credit quota.

    Each text chat session costs 1 credit; each voice session costs 3.
    Voice is also gated by the plan feature flag ``allow_voice``.

    Called from both HTTP dependencies (via check_conversation_quota) and
    WebSocket handlers (directly, after tenant resolution).
    """
    service = BillingService(db)

    try:
        sub = await service.get_subscription(tenant_id)
    except NotFoundError:
        # No subscription — apply free-tier cap (all-time total, no reset period).
        usage = await service.get_usage(tenant_id, "credits")
        if usage >= _FREE_TIER_LIMIT:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=(
                    f"Free tier limit of {_FREE_TIER_LIMIT} credits reached. "
                    "Please upgrade your plan to continue."
                ),
            )
        return

    # Inactive / cancelled subscription — block immediately.
    if sub.status not in ("active", "trialing"):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Your subscription is inactive. Please update your billing to continue.",
        )

    plan = await service.get_plan(sub.plan_id)
    if plan is None:
        # Plan row missing — let through rather than silently block a paying customer.
        return

    # Voice gate — check feature flag before spending credits.
    if is_voice:
        features = json.loads(plan.features or "{}")
        if not features.get("allow_voice", False):
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=(
                    "Voice is not available on your current plan. "
                    "Upgrade to Growth or Pro to enable voice."
                ),
            )

    # 0 means unlimited (reserved for future enterprise plans).
    if plan.max_conversations == 0:
        return

    usage = await service.get_usage_in_period(
        tenant_id, "credits", sub.current_period_start
    )
    if usage >= plan.max_conversations:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"Monthly credit limit of {plan.max_conversations} reached. "
                "Please upgrade your plan to continue."
            ),
        )


async def check_conversation_quota(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> None:
    """
    FastAPI dependency for widget-facing endpoints.

    Mirrors the same tenant resolution order as get_tenant_store_client:
      1. ?shop= query param (Shopify)
      2. X-Tenant-ID header (WooCommerce / non-Shopify)
      3. Dev/single-tenant fallback — no quota check applied

    Raises HTTP 402 if the resolved tenant has exceeded their quota.
    """
    repo = TenantRepository(db)

    shop = request.query_params.get("shop", "").strip()
    if shop:
        tenant = await repo.get_by_shopify_domain(shop)
        if tenant:
            await enforce_conversation_quota(tenant.id, db)
            return

    tenant_id = request.headers.get("X-Tenant-ID", "").strip()
    if tenant_id:
        tenant = await repo.get_by_id(tenant_id)
        if tenant and tenant.is_active:
            await enforce_conversation_quota(tenant.id, db)
            return

    # Global dev/single-tenant fallback — no quota enforcement.
