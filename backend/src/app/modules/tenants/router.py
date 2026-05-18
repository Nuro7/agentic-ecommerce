from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from .service import TenantService
from .schemas import TenantCreate, TenantOut, TenantUpdate
from ...core.database import get_db

router = APIRouter(prefix="/tenants", tags=["tenants"])


@router.post("/", response_model=TenantOut, status_code=201)
async def create_tenant(data: TenantCreate, db: AsyncSession = Depends(get_db)):
    return await TenantService(db).create_tenant(data)


@router.get("/", response_model=list[TenantOut])
async def list_tenants(skip: int = 0, limit: int = 50, db: AsyncSession = Depends(get_db)):
    return await TenantService(db).list_tenants(skip=skip, limit=limit)


@router.get("/{tenant_id}", response_model=TenantOut)
async def get_tenant(tenant_id: str, db: AsyncSession = Depends(get_db)):
    return await TenantService(db).get_tenant(tenant_id)


@router.patch("/{tenant_id}", response_model=TenantOut)
async def update_tenant(tenant_id: str, data: TenantUpdate, db: AsyncSession = Depends(get_db)):
    return await TenantService(db).update_tenant(tenant_id, data)
