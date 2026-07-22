from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from .repository import OfferRepository
from .models import ProductOffer


class OfferService:
    def __init__(self, db: AsyncSession):
        self.repo = OfferRepository(db)

    async def create_offer(
        self, tenant_id: str, data: dict
    ) -> ProductOffer:
        return await self.repo.create(tenant_id, data)

    async def update_offer(
        self, offer_id: str, tenant_id: str, data: dict
    ) -> Optional[ProductOffer]:
        offer = await self.repo.get_by_id(offer_id, tenant_id)
        if not offer:
            return None
        return await self.repo.update(offer, data)

    async def delete_offer(self, offer_id: str, tenant_id: str) -> bool:
        offer = await self.repo.get_by_id(offer_id, tenant_id)
        if not offer:
            return False
        await self.repo.delete(offer)
        return True

    async def list_offers(self, tenant_id: str) -> List[ProductOffer]:
        return await self.repo.list_by_tenant(tenant_id)

    async def get_active_promotions(
        self, tenant_id: str, limit: int = 5
    ) -> List[ProductOffer]:
        return await self.repo.get_active_promotions(tenant_id, limit)
