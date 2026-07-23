from typing import List, Optional
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timezone
from .models import ProductOffer


class OfferRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, tenant_id: str, data: dict) -> ProductOffer:
        offer = ProductOffer(tenant_id=tenant_id, **data)
        self.db.add(offer)
        await self.db.commit()
        await self.db.refresh(offer)
        return offer

    async def get_by_id(self, offer_id: str, tenant_id: str) -> Optional[ProductOffer]:
        stmt = select(ProductOffer).where(
            and_(ProductOffer.id == offer_id, ProductOffer.tenant_id == tenant_id)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_tenant(
        self, tenant_id: str, active_only: bool = False
    ) -> List[ProductOffer]:
        stmt = select(ProductOffer).where(ProductOffer.tenant_id == tenant_id)
        if active_only:
            now = datetime.now(timezone.utc)
            stmt = stmt.where(
                and_(
                    ProductOffer.is_active == True,
                    ProductOffer.starts_at <= now,
                    ProductOffer.ends_at >= now,
                )
            )
        stmt = stmt.order_by(ProductOffer.priority.desc(), ProductOffer.created_at.desc())
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def update(self, offer: ProductOffer, data: dict) -> ProductOffer:
        for key, value in data.items():
            if value is not None:
                setattr(offer, key, value)
        await self.db.commit()
        await self.db.refresh(offer)
        return offer

    async def delete(self, offer: ProductOffer) -> None:
        await self.db.delete(offer)
        await self.db.commit()

    async def get_active_promotions(
        self, tenant_id: str, limit: int = 5
    ) -> List[ProductOffer]:
        now = datetime.now(timezone.utc)
        stmt = (
            select(ProductOffer)
            .where(
                and_(
                    ProductOffer.tenant_id == tenant_id,
                    ProductOffer.is_active == True,
                    ProductOffer.starts_at <= now,
                    ProductOffer.ends_at >= now,
                )
            )
            .order_by(ProductOffer.priority.desc(), ProductOffer.created_at.desc())
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())
