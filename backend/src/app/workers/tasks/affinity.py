"""Celery task — build product_affinity from order_items (nightly)."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

from ..celery_app import celery_app
from ..utils import run_async
from ...core.database import worker_session as AsyncSessionLocal, set_tenant_guc
from ...modules.tenants.repository import TenantRepository

logger = logging.getLogger(__name__)

_SYNC_LOCK_TTL = 1800
_MIN_SUPPORT = 2


def _acquire_affinity_lock(key: str, ttl: int = _SYNC_LOCK_TTL):
    try:
        import redis as _redis
        from ...config import settings
        client = _redis.from_url(settings.redis_url)
        acquired = bool(client.set(key, "1", nx=True, ex=ttl))
        return client, acquired
    except Exception as exc:
        logger.warning("affinity lock unavailable (%s) — proceeding without lock", exc)
        return None, True


def _close_lock(client):
    if client is None:
        return
    try:
        client.close()
    except Exception:
        pass


@celery_app.task(
    name="src.app.workers.tasks.affinity.build_affinity",
    bind=True,
    max_retries=2,
    default_retry_delay=300,
)
def build_affinity(self, tenant_id: Optional[str] = None) -> dict:
    lock_key = f"speako:affinity_lock:{tenant_id or 'all'}"
    lock_client, acquired = _acquire_affinity_lock(lock_key)
    if not acquired:
        logger.info("Affinity build already running for %s — skipping", tenant_id or "all")
        _close_lock(lock_client)
        return {"skipped_duplicate": True, "tenants": 0, "pairs": 0}

    try:
        result = run_async(_build_affinity_async(tenant_id_filter=tenant_id))
        logger.info("Affinity build complete: tenants=%d pairs=%d", result["tenants"], result["pairs"])
        return result
    except Exception as exc:
        logger.error("Affinity build failed: %s", exc, exc_info=True)
        raise self.retry(exc=exc)
    finally:
        if lock_client is not None:
            try:
                lock_client.delete(lock_key)
            except Exception:
                pass
            _close_lock(lock_client)


async def _build_affinity_async(tenant_id_filter: Optional[str] = None) -> dict:
    total_pairs = 0
    tenants_processed = 0

    async with AsyncSessionLocal() as db:
        repo = TenantRepository(db)
        tenants = await repo.list_all(limit=500)

        for tenant in tenants:
            if tenant_id_filter and tenant.id != tenant_id_filter:
                continue
            if not tenant.is_active:
                continue

            await set_tenant_guc(db, tenant.id)

            try:
                pairs = await _build_tenant_affinity(db, tenant.id)
                await db.commit()  # isolate this tenant's work
                total_pairs += pairs
                tenants_processed += 1
                logger.info("Tenant %s affinity built: %d pairs", tenant.id, pairs)
            except Exception as exc:
                await db.rollback()  # discard only this tenant, keep going
                logger.warning("Affinity build failed for tenant=%s: %s", tenant.id, exc, exc_info=True)

    return {"tenants": tenants_processed, "pairs": total_pairs}


async def _build_tenant_affinity(db, tenant_id: str) -> int:
    await db.execute(
        text("DELETE FROM product_affinity WHERE tenant_id = :tid"),
        {"tid": tenant_id},
    )

    sql = text("""
        WITH op AS (
            SELECT DISTINCT o.id AS order_id, oi.product_id
            FROM orders o
            JOIN order_items oi ON oi.order_id = o.id
            WHERE o.tenant_id = :tid
              AND o.status = 'completed'
              AND oi.product_id IS NOT NULL
        )
        SELECT a.product_id AS product_id_a, b.product_id AS product_id_b, COUNT(*) AS co_count
        FROM op a
        JOIN op b ON a.order_id = b.order_id AND a.product_id < b.product_id
        GROUP BY a.product_id, b.product_id
        HAVING COUNT(*) >= :min_support
    """)
    result = await db.execute(sql, {"tid": tenant_id, "min_support": _MIN_SUPPORT})
    rows = result.all()

    if not rows:
        return 0

    upsert_sql = text("""
        INSERT INTO product_affinity (id, tenant_id, product_id_a, product_id_b, co_count, pair_support, last_computed_at)
        VALUES (:id, :tid, :a, :b, :co, :co, NOW())
        ON CONFLICT (tenant_id, product_id_a, product_id_b) DO UPDATE SET
            co_count = EXCLUDED.co_count,
            pair_support = EXCLUDED.pair_support,
            last_computed_at = NOW()
    """)
    for a, b, co in rows:
        await db.execute(upsert_sql, {
            "id": str(uuid.uuid4()),
            "tid": tenant_id,
            "a": a,
            "b": b,
            "co": co,
        })

    return len(rows)