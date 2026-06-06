from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from .repository import BillingRepository
from .models import Plan, UsageRecord
from ...core.exceptions import NotFoundError


class BillingService:
    def __init__(self, db: AsyncSession):
        self.repo = BillingRepository(db)

    async def list_plans(self) -> list[Plan]:
        return await self.repo.list_plans()

    async def get_subscription(self, tenant_id: str):
        sub = await self.repo.get_subscription(tenant_id)
        if not sub:
            raise NotFoundError()
        return sub

    async def get_plan(self, plan_id: str) -> Plan | None:
        return await self.repo.get_plan_by_id(plan_id)

    async def record_usage(self, tenant_id: str, metric: str, value: int = 1) -> None:
        record = UsageRecord(tenant_id=tenant_id, metric=metric, value=value)
        await self.repo.record_usage(record)

    async def get_usage(self, tenant_id: str, metric: str) -> int:
        return await self.repo.get_usage_total(tenant_id, metric)

    async def get_usage_in_period(self, tenant_id: str, metric: str, since: datetime) -> int:
        return await self.repo.get_usage_since(tenant_id, metric, since)
