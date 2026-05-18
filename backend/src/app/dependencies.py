from typing import Annotated
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from .core.database import get_db


async def get_current_tenant(
    x_tenant_id: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_db),
):
    if x_tenant_id is None:
        return None
    from .modules.tenants.repository import TenantRepository
    repo = TenantRepository(db)
    tenant = await repo.get_by_id(x_tenant_id)
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    return tenant
