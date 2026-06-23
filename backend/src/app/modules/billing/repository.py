from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from .models import Plan, Subscription, UsageRecord


class BillingRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_plan_by_name(self, name: str) -> Plan | None:
        result = await self.db.execute(select(Plan).where(Plan.name == name))
        return result.scalar_one_or_none()

    async def get_plan_by_id(self, plan_id: str) -> Plan | None:
        result = await self.db.execute(select(Plan).where(Plan.id == plan_id))
        return result.scalar_one_or_none()

    async def list_plans(self) -> list[Plan]:
        result = await self.db.execute(select(Plan))
        return list(result.scalars().all())

    async def get_subscription(self, tenant_id: str) -> Subscription | None:
        result = await self.db.execute(
            select(Subscription).where(Subscription.tenant_id == tenant_id)
        )
        return result.scalar_one_or_none()

    async def upsert_subscription(
        self,
        tenant_id: str,
        plan_id: str,
        status: str,
        period_start: datetime,
        period_end: datetime,
    ) -> Subscription:
        """Create or update the tenant's subscription (tenant_id is unique)."""
        sub = await self.get_subscription(tenant_id)
        if sub is None:
            sub = Subscription(
                tenant_id=tenant_id,
                plan_id=plan_id,
                status=status,
                current_period_start=period_start,
                current_period_end=period_end,
            )
            self.db.add(sub)
        else:
            sub.plan_id = plan_id
            sub.status = status
            sub.current_period_start = period_start
            sub.current_period_end = period_end
        await self.db.commit()
        await self.db.refresh(sub)
        return sub

    async def record_usage(self, record: UsageRecord) -> None:
        self.db.add(record)
        # Commit so usage is durably persisted; quota enforcement depends on it.
        # (Other callers in the request session have already committed their work.)
        await self.db.commit()

    async def get_usage_total(self, tenant_id: str, metric: str) -> int:
        result = await self.db.execute(
            select(func.sum(UsageRecord.value)).where(
                UsageRecord.tenant_id == tenant_id,
                UsageRecord.metric == metric,
            )
        )
        return result.scalar_one_or_none() or 0

    async def get_usage_since(self, tenant_id: str, metric: str, since: datetime) -> int:
        result = await self.db.execute(
            select(func.sum(UsageRecord.value)).where(
                UsageRecord.tenant_id == tenant_id,
                UsageRecord.metric == metric,
                UsageRecord.recorded_at >= since,
            )
        )
        return result.scalar_one_or_none() or 0

    async def list_active_subscriptions(self) -> list[Subscription]:
        result = await self.db.execute(
            select(Subscription).where(Subscription.status == "active")
        )
        return list(result.scalars().all())
