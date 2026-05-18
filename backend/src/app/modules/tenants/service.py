from sqlalchemy.ext.asyncio import AsyncSession
from .repository import TenantRepository
from .models import Tenant
from .schemas import TenantCreate, TenantUpdate
from ...core.exceptions import ConflictError, NotFoundError


class TenantService:
    def __init__(self, db: AsyncSession):
        self.repo = TenantRepository(db)

    async def create_tenant(self, data: TenantCreate) -> Tenant:
        existing = await self.repo.get_by_email(data.email)
        if existing:
            raise ConflictError()
        tenant = Tenant(**data.model_dump())
        return await self.repo.create(tenant)

    async def get_tenant(self, tenant_id: str) -> Tenant:
        tenant = await self.repo.get_by_id(tenant_id)
        if not tenant:
            raise NotFoundError()
        return tenant

    async def update_tenant(self, tenant_id: str, data: TenantUpdate) -> Tenant:
        tenant = await self.get_tenant(tenant_id)
        for field, value in data.model_dump(exclude_none=True).items():
            setattr(tenant, field, value)
        return await self.repo.update(tenant)

    async def list_tenants(self, skip: int = 0, limit: int = 50) -> list[Tenant]:
        return await self.repo.list_all(skip=skip, limit=limit)
