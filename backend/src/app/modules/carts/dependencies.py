from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from .service import CartService
from ...core.database import get_db


def get_cart_service(db: AsyncSession = Depends(get_db)) -> CartService:
    return CartService(db)
