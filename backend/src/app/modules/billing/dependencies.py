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
_PERIOD_TTL = 60 * 60 * 24 * 40  # 40 days — covers a monthly billing period


async def _reserve_credits(
    redis, tenant_id: str, *, cost: int, limit: int, period_key: str,
    db_usage_coro, ttl: int | None,
) -> bool:
    """Atomically reserve `cost` credits against `limit`. Returns True if allowed.

    Uses a Redis counter (seeded once from the DB ledger) so concurrent sessions
    cannot all pass a stale SUM check — the classic check-then-act overspend.
    Falls back to a (non-atomic) DB read when Redis is unavailable so the gate
    still works in degraded mode.
    """
    if redis is None:
        usage = await db_usage_coro()
        return usage + cost <= limit
    key = f"quota:{period_key}:{tenant_id}"
    try:
        if not await redis.exists(key):
            seed = int(await db_usage_coro() or 0)
            # NX so a concurrent seeder can't clobber an already-counted value.
            if ttl:
                await redis.set(key, seed, nx=True, ex=ttl)
            else:
                await redis.set(key, seed, nx=True)
        new = await redis.incrby(key, cost)
        if new > limit:
            await redis.decrby(key, cost)  # roll back the reservation
            return False
        return True
    except Exception:
        usage = await db_usage_coro()
        return usage + cost <= limit


async def enforce_conversation_quota(
    tenant_id: str,
    db: AsyncSession,
    *,
    is_voice: bool = False,
    redis=None,
) -> None:
    """
    Raise HTTP 402 if the tenant has exceeded their credit quota.

    Each text chat session costs 1 credit; each voice session costs 3.
    Voice is also gated by the plan feature flag ``allow_voice``.

    Enforcement atomically RESERVES the credits (via Redis) so concurrent
    sessions cannot collectively exceed the limit. The DB ``usage_records``
    ledger remains the source of truth for invoicing (written by record_usage).

    Called from both HTTP dependencies (via check_conversation_quota) and
    WebSocket handlers (directly, after tenant resolution).
    """
    service = BillingService(db)
    cost = 3 if is_voice else 1

    try:
        sub = await service.get_subscription(tenant_id)
    except NotFoundError:
        # No subscription — apply free-tier cap (all-time total, no reset period).
        allowed = await _reserve_credits(
            redis, tenant_id,
            cost=cost, limit=_FREE_TIER_LIMIT, period_key="credits:all",
            db_usage_coro=lambda: service.get_usage(tenant_id, "credits"),
            ttl=None,
        )
        if not allowed:
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

    period_key = f"credits:{sub.current_period_start.isoformat()}"
    allowed = await _reserve_credits(
        redis, tenant_id,
        cost=cost, limit=plan.max_conversations, period_key=period_key,
        db_usage_coro=lambda: service.get_usage_in_period(
            tenant_id, "credits", sub.current_period_start
        ),
        ttl=_PERIOD_TTL,
    )
    if not allowed:
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
    redis = getattr(request.app.state, "redis", None)

    shop = request.query_params.get("shop", "").strip()
    if shop:
        tenant = await repo.get_by_shopify_domain(shop)
        if tenant:
            await enforce_conversation_quota(tenant.id, db, redis=redis)
            return

    tenant_id = request.headers.get("X-Tenant-ID", "").strip()
    if tenant_id:
        tenant = await repo.get_by_id(tenant_id)
        if tenant and tenant.is_active:
            await enforce_conversation_quota(tenant.id, db, redis=redis)
            return

    # Global dev/single-tenant fallback — no quota enforcement.
