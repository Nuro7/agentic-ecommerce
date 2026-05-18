from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from .service import OrderService
from ...core.database import get_db


def get_order_service(db: AsyncSession = Depends(get_db)) -> OrderService:
    return OrderService(db)
