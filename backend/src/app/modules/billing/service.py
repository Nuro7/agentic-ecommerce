from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from .repository import BillingRepository
from .models import Plan, Subscription, UsageRecord
from ...config import settings
from ...core.exceptions import NotFoundError


class BillingService:
    def __init__(self, db: AsyncSession):
        self.repo = BillingRepository(db)

    async def list_plans(self) -> list[Plan]:
        return await self.repo.list_plans()

    async def assign_plan(self, tenant_id: str, plan: str) -> Subscription:
        """Activate (or change) a tenant's subscription to `plan` (name or id).

        Without a payment gateway, the subscription is activated immediately.
        Once BILLING_REQUIRE_PAYMENT is on, a paid plan lands in "pending" until
        the payment flow flips it to "active" (a free plan still activates).
        """
        resolved = await self.repo.get_plan_by_name(plan) or await self.repo.get_plan_by_id(plan)
        if resolved is None:
            err = NotFoundError()
            err.detail = f"Plan '{plan}' not found"
            raise err
        is_free = float(resolved.price_monthly or 0) == 0
        status = "active" if (not settings.billing_require_payment or is_free) else "pending"
        now = datetime.now(timezone.utc)
        return await self.repo.upsert_subscription(
            tenant_id=tenant_id,
            plan_id=resolved.id,
            status=status,
            period_start=now,
            period_end=now + timedelta(days=30),
        )

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
