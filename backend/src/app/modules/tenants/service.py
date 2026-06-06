from sqlalchemy.ext.asyncio import AsyncSession
from .repository import TenantRepository
from .models import Tenant
from .schemas import TenantCreate, TenantUpdate
from ...core.exceptions import ConflictError, NotFoundError

# Credential fields that invalidate the cached store client when changed.
_CREDENTIAL_FIELDS = {
    "platform",
    "shopify_domain", "shopify_access_token", "shopify_storefront_token",
    "woocommerce_store_url", "woocommerce_consumer_key", "woocommerce_consumer_secret",
    "custom_api_base_url", "custom_api_key",
}


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

        return tenant

    async def list_tenants(self, skip: int = 0, limit: int = 50) -> list[Tenant]:
        return await self.repo.list_all(skip=skip, limit=limit)
