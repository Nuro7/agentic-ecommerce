from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from .models import Order


class OrderRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, order: Order) -> Order:
        self.db.add(order)
        await self.db.commit()
        await self.db.refresh(order)
        return order

    async def get_by_id(self, order_id: str) -> Order | None:
        result = await self.db.execute(select(Order).where(Order.id == order_id))
        return result.scalar_one_or_none()

    async def get_by_platform_order_id(self, tenant_id: str, platform_order_id: str) -> Order | None:
        result = await self.db.execute(
            select(Order).where(
                Order.tenant_id == tenant_id,
                Order.platform_order_id == platform_order_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_by_tenant(self, tenant_id: str, skip: int = 0, limit: int = 50) -> list[Order]:
        result = await self.db.execute(
            select(Order).where(Order.tenant_id == tenant_id).offset(skip).limit(limit)
        )
        return list(result.scalars().all())

    async def update(self, order: Order) -> Order:
        await self.db.commit()
        await self.db.refresh(order)
        return order
