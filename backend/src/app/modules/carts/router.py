from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from .service import CartService
from .schemas import AddToCartRequest, CartOut
from ...core.database import get_db
from ..tenants.dependencies import get_authenticated_tenant

router = APIRouter(prefix="/carts", tags=["carts"])


@router.post("/items", status_code=201)
async def add_to_cart(
    data: AddToCartRequest,
    tenant=Depends(get_authenticated_tenant),
    db: AsyncSession = Depends(get_db),
):
    return await CartService(db).add_to_cart(tenant.id, data)


@router.get("/{session_id}", response_model=CartOut)
async def get_cart(
    session_id: str,
    tenant=Depends(get_authenticated_tenant),
    db: AsyncSession = Depends(get_db),
):
    return await CartService(db).get_cart(tenant.id, session_id)


@router.delete("/{session_id}", status_code=204)
async def clear_cart(
    session_id: str,
    tenant=Depends(get_authenticated_tenant),
    db: AsyncSession = Depends(get_db),
):
    await CartService(db).clear_cart(tenant.id, session_id)
