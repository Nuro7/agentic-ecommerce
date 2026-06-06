from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from .models import Tenant


class TenantRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_id(self, tenant_id: str) -> Tenant | None:
        result = await self.db.execute(select(Tenant).where(Tenant.id == tenant_id))
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> Tenant | None:
        result = await self.db.execute(select(Tenant).where(Tenant.email == email))
        return result.scalar_one_or_none()

    async def get_by_shopify_domain(self, domain: str) -> Tenant | None:
        result = await self.db.execute(
            select(Tenant).where(Tenant.shopify_domain == domain, Tenant.is_active == True)  # noqa: E712
        )
        return result.scalar_one_or_none()

    async def create(self, tenant: Tenant) -> Tenant:
        self.db.add(tenant)
        await self.db.commit()
        await self.db.refresh(tenant)
        return tenant

    async def update(self, tenant: Tenant) -> Tenant:
        await self.db.commit()
        await self.db.refresh(tenant)
        return tenant

    async def get_by_custom_api_key(self, api_key: str) -> Tenant | None:
        result = await self.db.execute(
            select(Tenant).where(
                Tenant.custom_api_key == api_key,
                Tenant.is_active == True,  # noqa: E712
            )
        )
        return result.scalar_one_or_none()

    async def list_all(self, skip: int = 0, limit: int = 50) -> list[Tenant]:
        result = await self.db.execute(select(Tenant).offset(skip).limit(limit))
        return list(result.scalars().all())
