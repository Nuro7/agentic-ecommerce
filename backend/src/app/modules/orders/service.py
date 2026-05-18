from sqlalchemy.ext.asyncio import AsyncSession
from .repository import OrderRepository
from .models import Order
from ...core.exceptions import NotFoundError


class OrderService:
    def __init__(self, db: AsyncSession):
        self.repo = OrderRepository(db)

    async def list_orders(self, tenant_id: str, skip: int = 0, limit: int = 50) -> list[Order]:
        return await self.repo.list_by_tenant(tenant_id, skip=skip, limit=limit)

    async def get_order(self, order_id: str) -> Order:
        order = await self.repo.get_by_id(order_id)
        if not order:
            raise NotFoundError()
        return order

    async def update_status(self, order_id: str, status: str) -> Order:
        order = await self.get_order(order_id)
        order.status = status
        return await self.repo.update(order)
