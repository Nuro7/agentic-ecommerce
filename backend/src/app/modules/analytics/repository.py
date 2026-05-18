from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from .models import ConversationMetric
from datetime import datetime


class AnalyticsRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_metrics(self, tenant_id: str, from_date: datetime, to_date: datetime) -> list[ConversationMetric]:
        result = await self.db.execute(
            select(ConversationMetric).where(
                ConversationMetric.tenant_id == tenant_id,
                ConversationMetric.date >= from_date,
                ConversationMetric.date <= to_date,
            ).order_by(ConversationMetric.date)
        )
        return list(result.scalars().all())

    async def get_totals(self, tenant_id: str) -> dict:
        result = await self.db.execute(
            select(
                func.sum(ConversationMetric.total_conversations),
                func.sum(ConversationMetric.completed_purchases),
                func.sum(ConversationMetric.revenue),
            ).where(ConversationMetric.tenant_id == tenant_id)
        )
        row = result.one()
        return {
            "total_conversations": row[0] or 0,
            "completed_purchases": row[1] or 0,
            "total_revenue": float(row[2] or 0),
        }
