"""
Tenant FastAPI dependencies.

get_tenant_store_client — resolves the calling tenant and returns their
    store client (ShopifyClient / CachedWooCommerceClient).

Resolution order:
  1. ?shop=<shopify_domain>   — Shopify widget identifies itself by shop domain
  2. X-Tenant-ID header       — explicit UUID for non-Shopify or server calls
  2b. ?tenant_id= query param — UUID set by the widget loader (WS path parity)
  3. app.state.store_client   — single-tenant / dev fallback (no DB lookup)

If no tenant is found the request gets a 503 so the widget shows an error
instead of silently using the wrong store's data.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from .repository import TenantRepository
from .service import TenantService
from ...config import settings
from ...core.database import get_db, set_request_tenant, set_tenant_guc
from ...core.security import decode_access_token
from ...integrations.factory import create_store_client_for_tenant, invalidate_tenant_client

logger = logging.getLogger(__name__)

# Sentinel tenant used for session/facts keys + token signing in NON-production when
# no real tenant resolves (dev/single-tenant). In production, requests with no
# resolvable tenant are rejected (see get_tenant_store_client / the WS handler), so
# this sentinel is never used in prod.
DEV_TENANT_ID = "_dev"


async def get_authenticated_tenant(
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Authenticate a merchant from a Bearer JWT and return their Tenant.

    Use this on merchant/admin endpoints (tenant settings, orders, analytics,
    billing). The tenant is derived from the signed token's `sub`, never from a
    client-supplied X-Tenant-ID header, so it cannot be spoofed.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[7:].strip()
    try:
        payload = decode_access_token(token)
    except Exception:
        # decode_access_token raises ValueError, but treat ANY decode failure as
        # 401 rather than leaking a 500 on an unexpected token/lib error.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    tenant_id = payload.get("sub")
    if not tenant_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token subject")
    tenant = await TenantRepository(db).get_by_id(tenant_id)
    if not tenant or not tenant.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Tenant not found or inactive")
    set_request_tenant(tenant.id)  # scope RLS for fresh sessions opened later in this request
    # Also apply the GUC to THIS (already-begun) request session — the endpoint reuses it
    # to read/write RLS tables (orders, cart_items, conversations), and the after_begin
    # event fired at GUC='' during the tenants lookup above.
    await set_tenant_guc(db, tenant.id)
    return tenant


async def require_tenant(
    x_tenant_id: str = Header(...),
    db: AsyncSession = Depends(get_db),
):
    """Strict: requires X-Tenant-ID header. Used by internal/admin endpoints.

    DEPRECATED for anything sensitive — the header is unauthenticated. Prefer
    get_authenticated_tenant on merchant/admin routes.
    """
    return await TenantService(db).get_tenant(x_tenant_id)


async def get_tenant_store_client(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """
    Resolve tenant → store client for widget-facing endpoints.

    Falls back to app.state.store_client so single-tenant / dev mode
    still works without a DB entry.
    """
    redis = getattr(request.app.state, "redis", None)
    repo = TenantRepository(db)

    # 1. ?shop= (Shopify domain in query string — set by widget loader)
    shop = request.query_params.get("shop", "").strip()
    if shop:
        tenant = await repo.get_by_shopify_domain(shop)
        if tenant:
            logger.debug("Tenant resolved via shop=%s → tenant_id=%s", shop, tenant.id)
            set_request_tenant(tenant.id)        # fresh sessions opened later this request
            await set_tenant_guc(db, tenant.id)  # the already-begun request session too
            return await create_store_client_for_tenant(tenant, redis_client=redis)
        logger.warning("shop=%s not found in DB — falling back to app.state", shop)

    # 2. X-Tenant-ID header
    tenant_id = request.headers.get("X-Tenant-ID", "").strip()
    if tenant_id:
        tenant = await repo.get_by_id(tenant_id)
        if tenant and tenant.is_active:
            logger.debug("Tenant resolved via X-Tenant-ID=%s", tenant_id)
            set_request_tenant(tenant.id)        # fresh sessions opened later this request
            await set_tenant_guc(db, tenant.id)  # the already-begun request session too
            return await create_store_client_for_tenant(tenant, redis_client=redis)
        logger.warning("X-Tenant-ID=%s not found or inactive", tenant_id)

    # 2b. ?tenant_id= (UUID in query string — set by the widget loader; the
    # WebSocket path already reads this, see resolve_tenant_store_client_for_ws).
    qp_tenant_id = request.query_params.get("tenant_id", "").strip()
    if qp_tenant_id:
        tenant = await repo.get_by_id(qp_tenant_id)
        if tenant and tenant.is_active:
            logger.debug("Tenant resolved via ?tenant_id=%s", qp_tenant_id)
            set_request_tenant(tenant.id)        # fresh sessions opened later this request
            await set_tenant_guc(db, tenant.id)  # the already-begun request session too
            return await create_store_client_for_tenant(tenant, redis_client=redis)
        logger.warning("?tenant_id=%s not found or inactive", qp_tenant_id)

    # 3. No tenant resolved. In production (or when enforcement is explicitly on) we
    # MUST NOT fall back to a shared global client — two tenants arriving without a
    # shop/tenant would collapse onto the same store and merge their data. Reject.
    if settings.require_tenant:
        logger.warning("Unresolved tenant (enforced) — rejecting request")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unresolved tenant",
        )

    # 4. Global fallback — single-tenant / dev mode only
    store_client = getattr(request.app.state, "store_client", None)
    if store_client:
        return store_client

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Store not configured for this request.",
    )


async def resolve_tenant_id_from_request(request: Request, db: AsyncSession) -> str | None:
    """Resolve the calling tenant's id (real tenant only) from ?shop= / X-Tenant-ID / ?tenant_id=.

    Returns None when no real tenant resolves. Callers that need a key-safe value in
    dev use `resolved or DEV_TENANT_ID`; billing/usage paths use the real id or skip.
    """
    repo = TenantRepository(db)
    shop = request.query_params.get("shop", "").strip()
    if shop:
        tenant = await repo.get_by_shopify_domain(shop)
        if tenant:
            set_request_tenant(tenant.id)
            await set_tenant_guc(db, tenant.id)  # scope the already-begun request session
            return tenant.id
    tenant_id = request.headers.get("X-Tenant-ID", "").strip()
    if tenant_id:
        tenant = await repo.get_by_id(tenant_id)
        if tenant and tenant.is_active:
            set_request_tenant(tenant.id)
            await set_tenant_guc(db, tenant.id)  # scope the already-begun request session
            return tenant.id
    qp_tenant_id = request.query_params.get("tenant_id", "").strip()
    if qp_tenant_id:
        tenant = await repo.get_by_id(qp_tenant_id)
        if tenant and tenant.is_active:
            set_request_tenant(tenant.id)
            await set_tenant_guc(db, tenant.id)  # scope the already-begun request session
            return tenant.id
    return None


async def resolve_tenant_store_client_for_ws(
    shop: str,
    tenant_id: str,
    app_state: Any,
    db: AsyncSession,
) -> tuple[Any, str | None]:
    """
    WebSocket variant — called inside the handler where Depends() isn't available.

    Mirrors get_tenant_store_client resolution order.
    Returns (store_client, resolved_tenant_id). tenant_id is None when falling
    back to the global app.state.store_client (dev/single-tenant mode).
    """
    redis = getattr(app_state, "redis", None)
    repo = TenantRepository(db)

    if shop:
        tenant = await repo.get_by_shopify_domain(shop)
        if tenant:
            return await create_store_client_for_tenant(tenant, redis_client=redis), tenant.id

    if tenant_id:
        tenant = await repo.get_by_id(tenant_id)
        if tenant and tenant.is_active:
            return await create_store_client_for_tenant(tenant, redis_client=redis), tenant.id

    return getattr(app_state, "store_client", None), None
