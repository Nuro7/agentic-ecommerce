"""Celery task — monthly subscription invoicing (runs 1st of month, 02:00 UTC)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from ..celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    name="src.app.workers.tasks.billing.run_monthly_invoicing",
    bind=True,
    max_retries=2,
    default_retry_delay=600,
)
def run_monthly_invoicing(self) -> dict:
    """Iterate active subscriptions and record monthly usage invoice events.

    Runs on the 1st of each month at 02:00 UTC via Celery beat.
    Records a usage_record per tenant so the billing dashboard can show
    monthly spend. Actual payment gateway charging is a future step.
    """
    try:
        result = asyncio.run(_invoice_async())
        logger.info("Billing task: invoiced %d tenants", result["invoiced"])
        return result
    except Exception as exc:
        logger.error("Billing invoicing failed: %s", exc, exc_info=True)
        raise self.retry(exc=exc)


async def _invoice_async() -> dict:
    from ...core.database import AsyncSessionLocal
    from ...modules.billing.repository import BillingRepository
    from ...modules.billing.models import UsageRecord

    now = datetime.now(tz=timezone.utc)
    invoiced = 0
    skipped = 0

    async with AsyncSessionLocal() as db:
        repo = BillingRepository(db)
        subscriptions = await repo.list_active_subscriptions()

        for sub in subscriptions:
            try:
                # Record a monthly invoice usage event for audit / dashboard
                record = UsageRecord(
                    tenant_id=sub.tenant_id,
                    metric="monthly_invoice",
                    value=1,
                )
                await repo.record_usage(record)
                invoiced += 1
                logger.debug("Invoiced tenant=%s plan=%s", sub.tenant_id, sub.plan_id)
            except Exception as exc:
                logger.warning("Failed to invoice tenant=%s: %s", sub.tenant_id, exc)
                skipped += 1

        await db.commit()

    return {
        "invoiced": invoiced,
        "skipped": skipped,
        "month": now.strftime("%Y-%m"),
    }
