from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from .models import CartItem


class CartRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def add_item(self, item: CartItem) -> CartItem:
        self.db.add(item)
        await self.db.commit()
        await self.db.refresh(item)
        return item

    async def get_items(self, tenant_id: str, session_id: str) -> list[CartItem]:
        result = await self.db.execute(
            select(CartItem).where(
                CartItem.tenant_id == tenant_id,
                CartItem.session_id == session_id,
            )
        )
        return list(result.scalars().all())

    async def remove_item(self, item_id: str) -> None:
        await self.db.execute(delete(CartItem).where(CartItem.id == item_id))
        await self.db.commit()

    async def clear(self, tenant_id: str, session_id: str) -> None:
        await self.db.execute(
            delete(CartItem).where(
                CartItem.tenant_id == tenant_id,
                CartItem.session_id == session_id,
            )
        )
        await self.db.commit()
