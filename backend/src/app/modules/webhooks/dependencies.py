from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from .service import WebhookService
from ...core.database import get_db


def get_webhook_service(db: AsyncSession = Depends(get_db)) -> WebhookService:
    return WebhookService(db)
