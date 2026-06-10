from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from .service import ProductService
from .schemas import ProductSearchRequest, ProductOut
from ...core.database import get_db
from ..tenants.dependencies import get_authenticated_tenant

router = APIRouter(prefix="/products", tags=["products"])


@router.post("/search", response_model=list[ProductOut])
async def search_products(
    data: ProductSearchRequest,
    tenant=Depends(get_authenticated_tenant),
    db: AsyncSession = Depends(get_db),
):
    return await ProductService(db).search_products(tenant.id, data.query, data.limit)
