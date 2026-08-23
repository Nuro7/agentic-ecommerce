from typing import List
from sqlalchemy.ext.asyncio import AsyncSession
from .repository import CartRepository
from .models import CartItem
from .schemas import AddToCartRequest, CartOut, CartItemOut, CartDiscountOut
from ..offers.repository import OfferRepository
from ..offers.engine import evaluate_cart


class CartService:
    def __init__(self, db: AsyncSession):
        self.db = db
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

        cart_lines = [
            {
                "platform_product_id": i.platform_product_id,
                "quantity": i.quantity,
                "unit_price": float(i.unit_price),
                "name": i.name,
            }
            for i in items
        ]

        discounts = []
        savings = 0.0
        try:
            active_offers = await OfferRepository(self.db).get_active_offers(tenant_id)
            if active_offers:
                result = evaluate_cart(cart_lines, active_offers)
                discounts = [
                    CartDiscountOut(
                        title=d.get("title", ""),
                        kind=d.get("kind", d.get("offer_kind", "discount")),
                        discount=float(d.get("discount", 0)),
                        savings=float(d.get("savings", d.get("discount", 0))),
                        offer_id=d.get("offer_id"),
                        discount_code=d.get("discount_code"),
                    )
                    for d in result.get("discounts", [])
                ]
                savings = float(result.get("savings", 0))
                subtotal = float(result.get("subtotal", 0))
                total = float(result.get("total", 0))
            else:
                subtotal = round(sum(i.quantity * float(i.unit_price) for i in items), 2)
                total = subtotal
        except Exception:
            # Rule engine must never break cart reads — fall back to raw totals.
            subtotal = round(sum(i.quantity * float(i.unit_price) for i in items), 2)
            total = subtotal

        return CartOut(
            items=[CartItemOut.model_validate(i) for i in items],
            subtotal=subtotal,
            discounts=discounts,
            savings=savings,
            total=total,
        )

    async def clear_cart(self, tenant_id: str, session_id: str) -> None:
        await self.repo.clear(tenant_id, session_id)