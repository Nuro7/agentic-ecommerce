from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from .service import TenantService
from ...core.database import get_db


async def require_tenant(
    x_tenant_id: str = Header(...),
    db: AsyncSession = Depends(get_db),
):
    return await TenantService(db).get_tenant(x_tenant_id)
