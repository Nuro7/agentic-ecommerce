from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from .service import AnalyticsService
from ...core.database import get_db


def get_analytics_service(db: AsyncSession = Depends(get_db)) -> AnalyticsService:
    return AnalyticsService(db)
