from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from .service import OrderService
from .schemas import OrderOut, OrderStatusUpdate
from ...core.database import get_db
from ..tenants.dependencies import get_authenticated_tenant

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("/", response_model=list[OrderOut])
async def list_orders(
    skip: int = 0,
    limit: int = 50,
    tenant=Depends(get_authenticated_tenant),
    db: AsyncSession = Depends(get_db),
):
    return await OrderService(db).list_orders(tenant.id, skip=skip, limit=limit)


@router.get("/{order_id}", response_model=OrderOut)
async def get_order(
    order_id: str,
    tenant=Depends(get_authenticated_tenant),
    db: AsyncSession = Depends(get_db),
):
    order = await OrderService(db).get_order(order_id)
    if order.tenant_id != tenant.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return order


@router.patch("/{order_id}/status", response_model=OrderOut)
async def update_status(
    order_id: str,
    data: OrderStatusUpdate,
    tenant=Depends(get_authenticated_tenant),
    db: AsyncSession = Depends(get_db),
):
    order = await OrderService(db).get_order(order_id)
    if order.tenant_id != tenant.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return await OrderService(db).update_status(order_id, data.status)
