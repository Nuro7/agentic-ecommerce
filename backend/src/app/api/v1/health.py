from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.cache import get_redis
from ...core.database import get_db
from ...config import settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    try:
        r = get_redis()
        await r.ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    return {"status": "ok", "redis": redis_ok}


@router.get("/ops")
async def ops(db: AsyncSession = Depends(get_db)):
    """Operational metrics for monitoring/alerting.

    Surfaces the signals that would have flagged the inert-worker condition:
    queue depth, dead-letter size, and pending-webhook backlog age.
    """
    from ...agent.retry_queue import _QUEUE_KEY, _DEAD_KEY

    out: dict = {"status": "ok"}

    try:
        r = get_redis()
        await r.ping()
        out["redis"] = True
        try:
            out["retry_queue_depth"] = await r.zcard(_QUEUE_KEY)
            out["dead_letter_size"] = await r.zcard(_DEAD_KEY)
            out["celery_queue_depth"] = await r.llen("celery")
        except Exception as exc:
            out["redis_metrics_error"] = str(exc)
    except Exception:
        out["redis"] = False

    # DB connectivity (the webhook query below would also fail, but make it explicit).
    try:
        await db.execute(text("SELECT 1"))
        out["db"] = True
    except Exception:
        out["db"] = False

    # Webhook backlog — a growing pending count / old oldest-age means the worker
    # (process_pending) isn't running or is falling behind.
    try:
        row = (
            await db.execute(
                text(
                    "SELECT count(*) AS pending, "
                    "EXTRACT(EPOCH FROM (now() - min(received_at))) AS oldest_age_s "
                    "FROM webhook_events WHERE status = 'pending'"
                )
            )
        ).one()
        out["webhooks_pending"] = int(row.pending or 0)
        out["webhooks_oldest_age_s"] = float(row.oldest_age_s) if row.oldest_age_s is not None else None
    except Exception as exc:
        out["webhook_metrics_error"] = str(exc)

    # Product-sync staleness — if the newest cached product is old, or many tenants
    # are stale (>48h, matching _cleanup_deleted), product sync isn't running.
    try:
        row = (
            await db.execute(
                text(
                    "SELECT EXTRACT(EPOCH FROM (now() - max(cached_at))) AS newest_age_s, "
                    "count(DISTINCT tenant_id) FILTER "
                    "(WHERE cached_at < now() - interval '48 hours') AS stale_tenants "
                    "FROM product_cache"
                )
            )
        ).one()
        out["sync_newest_age_s"] = float(row.newest_age_s) if row.newest_age_s is not None else None
        out["sync_stale_tenants"] = int(row.stale_tenants or 0)
    except Exception as exc:
        out["sync_metrics_error"] = str(exc)

    # Embeddings provider health — data-derived (NO live API call, so /ops stays cheap).
    # A high NULL-embedding ratio among recently-synced in-stock rows ⇒ the embeddings
    # provider was failing during recent syncs. Only meaningful when a key is configured.
    if settings.openai_api_key:
        out["embeddings_provider"] = "openai"
        try:
            row = (
                await db.execute(
                    text(
                        "SELECT count(*) FILTER (WHERE embedding IS NULL) AS missing, "
                        "count(*) AS total FROM product_cache "
                        "WHERE cached_at > now() - interval '24 hours' AND in_stock IS NOT FALSE"
                    )
                )
            ).one()
            total = int(row.total or 0)
            out["embeddings_recent_total"] = total
            out["embeddings_missing_ratio"] = (int(row.missing or 0) / total) if total else None
        except Exception as exc:
            out["embeddings_metrics_error"] = str(exc)
    else:
        out["embeddings_provider"] = None

    return out
