from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.cache import get_redis
from ...core.database import get_db

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

    return out
