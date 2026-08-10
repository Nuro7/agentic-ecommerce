from typing import List, Optional
from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timezone
from .models import ProductOffer


def _not_exhausted() -> "Optional[object]":
    return or_(
        ProductOffer.max_redemptions.is_(None),
        ProductOffer.redemption_count < ProductOffer.max_redemptions,
    )


def _within_active_period(now: datetime) -> "object":
    """starts_at/ends_at are nullable — NULL means no restriction.

    ``col <= now`` with a NULL column evaluates to NULL (falsy), which would
    silently drop every offer created without explicit dates. Wrap each bound
    in an or_() so NULL dates keep the offer active.
    """
    return and_(
        or_(ProductOffer.starts_at.is_(None), ProductOffer.starts_at <= now),
        or_(ProductOffer.ends_at.is_(None), ProductOffer.ends_at >= now),
    )


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
                    _within_active_period(now),
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

    async def increment_redemptions(self, offer_id: str, tenant_id: str, amount: int = 1) -> None:
        """Increment redemption_count so max_redemptions caps are enforced."""
        offer = await self.get_by_id(offer_id, tenant_id)
        if not offer:
            return
        offer.redemption_count = int(offer.redemption_count or 0) + amount
        await self.db.commit()

    async def get_active_offers(
        self, tenant_id: str, kinds: Optional[List[str]] = None
    ) -> List[ProductOffer]:
        """All non-exhausted active offers (any offer_kind) for the rule engine."""
        now = datetime.now(timezone.utc)
        filters = [
            ProductOffer.tenant_id == tenant_id,
            ProductOffer.is_active == True,
            _within_active_period(now),
            _not_exhausted(),
        ]
        if kinds:
            filters.append(ProductOffer.offer_kind.in_(kinds))
        stmt = (
            select(ProductOffer)
            .where(and_(*filters))
            .order_by(ProductOffer.priority.desc(), ProductOffer.created_at.desc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

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
                    _within_active_period(now),
                    _not_exhausted(),
                )
            )
            .order_by(ProductOffer.priority.desc(), ProductOffer.created_at.desc())
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())
