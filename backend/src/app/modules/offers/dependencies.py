from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from ...core.database import get_db
from .service import OfferService


async def get_offer_service(db: AsyncSession = Depends(get_db)) -> OfferService:
    return OfferService(db)
