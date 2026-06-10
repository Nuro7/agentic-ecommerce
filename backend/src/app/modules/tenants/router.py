from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from .service import TenantService
from .schemas import TenantOut, TenantUpdate
from .dependencies import get_authenticated_tenant
from ...core.database import get_db

router = APIRouter(prefix="/tenants", tags=["tenants"])


# NOTE: tenant self-service signup is handled by the public POST /api/v1/onboard
# endpoint. These routes are the authenticated merchant dashboard surface — a
# merchant may only read/update their OWN tenant. Creating/listing arbitrary
# tenants is intentionally not exposed here (was an unauthenticated IDOR before).


@router.get("/me", response_model=TenantOut)
async def get_my_tenant(tenant=Depends(get_authenticated_tenant)):
    return tenant


@router.get("/{tenant_id}", response_model=TenantOut)
async def get_tenant(
    tenant_id: str,
    tenant=Depends(get_authenticated_tenant),
):
    if tenant_id != tenant.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    return tenant


@router.patch("/{tenant_id}", response_model=TenantOut)
async def update_tenant(
    tenant_id: str,
    data: TenantUpdate,
    tenant=Depends(get_authenticated_tenant),
    db: AsyncSession = Depends(get_db),
):
    if tenant_id != tenant.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    return await TenantService(db).update_tenant(tenant_id, data)
