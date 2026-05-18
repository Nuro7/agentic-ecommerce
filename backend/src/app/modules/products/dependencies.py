from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from .service import ProductService
from ...core.database import get_db


def get_product_service(db: AsyncSession = Depends(get_db)) -> ProductService:
    return ProductService(db)
