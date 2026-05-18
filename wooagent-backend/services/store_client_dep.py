"""
services/store_client_dep.py

FastAPI dependency that returns the correct store client for a request.

In single-tenant mode (no X-Tenant-ID header): returns app.state.woo_client.
In multi-tenant SaaS mode (X-Tenant-ID present): resolves credentials from
the shared PostgreSQL database and returns a dynamically created client.

Usage in any router:
    from services.store_client_dep import StoreClientDep

    @router.post("/chat")
    async def chat(req: Request, store_client: StoreClientDep, ...):
        products = await store_client.search_products(query="...")
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Header, Request

from services.tenant_resolver import get_store_client_for_tenant


async def resolve_store_client(
    request: Request,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-ID")] = None,
) -> Any:
    """
    Return the store client appropriate for this request.
    - With X-Tenant-ID header → look up tenant's store from DB
    - Without header → fall back to the singleton client from app.state
    """
    fallback = getattr(request.app.state, "woo_client", None)

    if not x_tenant_id:
        return fallback

    redis = getattr(request.app.state, "redis", None)
    return await get_store_client_for_tenant(
        tenant_id=x_tenant_id,
        redis_client=redis,
        fallback_client=fallback,
    )


StoreClientDep = Annotated[Any, Depends(resolve_store_client)]
