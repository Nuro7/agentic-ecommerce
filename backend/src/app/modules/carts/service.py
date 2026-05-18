from sqlalchemy.ext.asyncio import AsyncSession
from .repository import CartRepository
from .models import CartItem
from .schemas import AddToCartRequest, CartOut, CartItemOut


class CartService:
    def __init__(self, db: AsyncSession):
        self.repo = CartRepository(db)

    async def add_to_cart(self, tenant_id: str, data: AddToCartRequest) -> CartItem:
        item = CartItem(
            tenant_id=tenant_id,
            session_id=data.session_id,
            platform_product_id=data.platform_product_id,
            variant_id=data.variant_id,
            name=data.name,
            quantity=data.quantity,
            unit_price=data.unit_price,
        )
        return await self.repo.add_item(item)

    async def get_cart(self, tenant_id: str, session_id: str) -> CartOut:
        items = await self.repo.get_items(tenant_id, session_id)
        total = sum(i.quantity * float(i.unit_price) for i in items)
        return CartOut(items=[CartItemOut.model_validate(i) for i in items], total=round(total, 2))

    async def clear_cart(self, tenant_id: str, session_id: str) -> None:
        await self.repo.clear(tenant_id, session_id)
