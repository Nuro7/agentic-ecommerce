"""
Tenant FastAPI dependencies.

get_tenant_store_client — resolves the calling tenant and returns their
    store client (ShopifyClient / CachedWooCommerceClient).

Resolution order:
  1. ?shop=<shopify_domain>   — Shopify widget identifies itself by shop domain
  2. X-Tenant-ID header       — explicit UUID for non-Shopify or server calls
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
from ...core.database import get_db
from ...integrations.factory import create_store_client_for_tenant, invalidate_tenant_client

logger = logging.getLogger(__name__)


async def require_tenant(
    x_tenant_id: str = Header(...),
    db: AsyncSession = Depends(get_db),
):
    """Strict: requires X-Tenant-ID header. Used by internal/admin endpoints."""
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
            return await create_store_client_for_tenant(tenant, redis_client=redis)
        logger.warning("shop=%s not found in DB — falling back to app.state", shop)

    # 2. X-Tenant-ID header
    tenant_id = request.headers.get("X-Tenant-ID", "").strip()
    if tenant_id:
        tenant = await repo.get_by_id(tenant_id)
        if tenant and tenant.is_active:
            logger.debug("Tenant resolved via X-Tenant-ID=%s", tenant_id)
            return await create_store_client_for_tenant(tenant, redis_client=redis)
        logger.warning("X-Tenant-ID=%s not found or inactive", tenant_id)

    # 3. Global fallback — single-tenant / dev mode
    store_client = getattr(request.app.state, "store_client", None)
    if store_client:
        return store_client

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Store not configured for this request.",
    )


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
