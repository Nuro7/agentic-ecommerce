from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from .service import OrderService
from .schemas import OrderOut, OrderStatusUpdate
from ...core.database import get_db
from ..tenants.dependencies import require_tenant

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("/", response_model=list[OrderOut])
async def list_orders(
    skip: int = 0,
    limit: int = 50,
    tenant=Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
):
    return await OrderService(db).list_orders(tenant.id, skip=skip, limit=limit)


@router.get("/{order_id}", response_model=OrderOut)
async def get_order(order_id: str, db: AsyncSession = Depends(get_db)):
    return await OrderService(db).get_order(order_id)


@router.patch("/{order_id}/status", response_model=OrderOut)
async def update_status(
    order_id: str,
    data: OrderStatusUpdate,
    db: AsyncSession = Depends(get_db),
):
    return await OrderService(db).update_status(order_id, data.status)
