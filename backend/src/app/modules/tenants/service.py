import logging
import time
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from .repository import TenantRepository
from .models import Tenant
from .schemas import TenantCreate, TenantUpdate
from ...core.exceptions import ConflictError, NotFoundError

logger = logging.getLogger(__name__)

# Credential fields that invalidate the cached store client when changed.
_CREDENTIAL_FIELDS = {
    "platform",
    "shopify_domain", "shopify_access_token", "shopify_storefront_token",
    "woocommerce_store_url", "woocommerce_consumer_key", "woocommerce_consumer_secret",
    "custom_api_base_url", "custom_api_key",
}

# ── Per-tenant store config (currency + policies + about) ─────────────────────
# Resolution contract: callers use `cfg[key] or <their existing env/default>`,
# so a NULL column keeps today's env-var behavior exactly. Values are cached
# in-process for 5 minutes (same pattern as the store-catalog cache).

_STORE_CONFIG_FIELDS = (
    "currency_symbol", "shipping_policy", "returns_policy",
    "payment_methods", "about_text",
)
_STORE_CONFIG_TTL = 300.0
_store_config_cache: dict[str, tuple[float, dict]] = {}


def resolve_store_config(tenant: Optional[Tenant]) -> dict:
    """Extract the per-tenant store config from a Tenant row (None when unset)."""
    cfg: dict = {}
    for field in _STORE_CONFIG_FIELDS:
        val = getattr(tenant, field, None) if tenant is not None else None
        cfg[field] = str(val).strip() if val and str(val).strip() else None
    name = getattr(tenant, "name", None) if tenant is not None else None
    cfg["store_name"] = str(name).strip() if name and str(name).strip() else None
    return cfg


def _empty_store_config() -> dict:
    return {field: None for field in (*_STORE_CONFIG_FIELDS, "store_name")}


async def get_store_config_for_tenant(tenant_id: Optional[str]) -> dict:
    """Fetch + cache a tenant's store config. Never raises — returns an
    all-None dict on any failure so callers fall back to env defaults."""
    if not tenant_id:
        return _empty_store_config()
    now = time.monotonic()
    hit = _store_config_cache.get(tenant_id)
    if hit and now - hit[0] < _STORE_CONFIG_TTL:
        return hit[1]
    try:
        from ...core.database import AsyncSessionLocal
        async with AsyncSessionLocal() as session:
            tenant = await TenantRepository(session).get_by_id(tenant_id)
        cfg = resolve_store_config(tenant) if tenant else _empty_store_config()
    except Exception as exc:
        logger.debug("Store config lookup failed tenant=%s: %s", tenant_id, exc)
        return hit[1] if hit else _empty_store_config()
    _store_config_cache[tenant_id] = (now, cfg)
    return cfg


def invalidate_store_config(tenant_id: str) -> None:
    _store_config_cache.pop(tenant_id, None)


class TenantService:
    def __init__(self, db: AsyncSession):
        self.repo = TenantRepository(db)

    async def create_tenant(self, data: TenantCreate) -> Tenant:
        existing = await self.repo.get_by_email(data.email)
        if existing:
            raise ConflictError()
        tenant = Tenant(**data.model_dump(exclude_none=True))
        return await self.repo.create(tenant)

    async def get_tenant(self, tenant_id: str) -> Tenant:
        tenant = await self.repo.get_by_id(tenant_id)
        if not tenant:
            raise NotFoundError()
        return tenant

    async def update_tenant(self, tenant_id: str, data: TenantUpdate) -> Tenant:
        tenant = await self.get_tenant(tenant_id)
        updates = data.model_dump(exclude_none=True)

        for field, value in updates.items():
            setattr(tenant, field, value)

        tenant = await self.repo.update(tenant)

        # Invalidate cached store client if any credential field changed.
        if _CREDENTIAL_FIELDS & updates.keys():
            from ...integrations.factory import invalidate_tenant_client
            await invalidate_tenant_client(tenant_id)

        # Any update may change name/config fields — drop the store-config cache.
        invalidate_store_config(tenant_id)

        return tenant

    async def list_tenants(self, skip: int = 0, limit: int = 50) -> list[Tenant]:
        return await self.repo.list_all(skip=skip, limit=limit)
