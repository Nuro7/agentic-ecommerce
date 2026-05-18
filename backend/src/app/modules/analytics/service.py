from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from .repository import AnalyticsRepository
from .schemas import AnalyticsSummary


class AnalyticsService:
    def __init__(self, db: AsyncSession):
        self.repo = AnalyticsRepository(db)

    async def get_summary(self, tenant_id: str) -> AnalyticsSummary:
        totals = await self.repo.get_totals(tenant_id)
        convs = totals["total_conversations"]
        purchases = totals["completed_purchases"]
        rate = round(purchases / convs * 100, 2) if convs else 0.0
        return AnalyticsSummary(
            total_conversations=convs,
            completed_purchases=purchases,
            total_revenue=totals["total_revenue"],
            conversion_rate=rate,
        )

    async def get_metrics(self, tenant_id: str, from_date: datetime, to_date: datetime):
        return await self.repo.list_metrics(tenant_id, from_date, to_date)
