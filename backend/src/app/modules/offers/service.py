from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from .repository import OfferRepository
from .models import ProductOffer
from .from_text import parse_offer_from_text
from .schemas import ProductOfferCreate
from .discounts import ensure_offer_has_bound_code


class OfferService:
    def __init__(self, db: AsyncSession):
        self.repo = OfferRepository(db)

    async def create_offer(
        self, tenant_id: str, data: dict
    ) -> ProductOffer:
        offer = await self.repo.create(tenant_id, data)
        if offer.offer_kind in ("combo", "bulk"):
            await ensure_offer_has_bound_code(tenant_id, offer.id)
        return offer

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

    async def get_active_offers(
        self, tenant_id: str, kinds: Optional[List[str]] = None
    ) -> List[ProductOffer]:
        """Active, non-exhausted offers for the rule engine / cart evaluation."""
        return await self.repo.get_active_offers(tenant_id, kinds)

    async def create_from_text(self, tenant_id: str, text: str) -> ProductOffer:
        """Parse a merchant's plain-English offer and persist it.

        Raises ValueError when the text cannot be parsed into a valid offer.
        """
        data = await parse_offer_from_text(text)
        valid = ProductOfferCreate.model_validate(data)
        offer = await self.repo.create(tenant_id, valid.model_dump())
        if offer.offer_kind in ("combo", "bulk"):
            await ensure_offer_has_bound_code(tenant_id, offer.id)
        return offer
