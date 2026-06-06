"""Celery task — retry failed mutation tool calls (Phase 10).

Periodically drains the speako:retry_queue Redis sorted set, re-executing
tool calls that failed during a live agent session (add_to_cart, apply_coupon,
etc.).  Uses the same execute_tool() dispatcher as the orchestrator so retry
behaviour is identical to the original call.

Schedule: every 60 s (set in schedules.py) — same cadence as webhook processing.

Back-off (managed by retry_queue.py):
  attempt 1 failed → retry after 30 s
  attempt 2 failed → retry after 5 min
  attempt 3 failed → dead-letter (no further retries)

Retry result logging
--------------------
Success and permanent failure are logged at INFO / WARNING level.  There is no
real-time push back to the user's active session in V1 (no persistent WebSocket
connection between Celery and the widget).  The user will see the correct state
on their next cart view or message.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from ..celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    name="src.app.workers.tasks.retry_actions.retry_failed_actions",
    bind=True,
    max_retries=0,   # the task itself doesn't retry — items do via requeue
    ignore_result=True,
)
def retry_failed_actions(self) -> dict:
    """Drain items due in speako:retry_queue and re-execute them."""
    try:
        result = asyncio.run(_retry_async())
        logger.info(
            "retry_failed_actions: processed=%d succeeded=%d requeued=%d dead=%d",
            result["processed"], result["succeeded"], result["requeued"], result["dead"],
        )
        return result
    except Exception as exc:
        logger.error("retry_failed_actions crashed: %s", exc, exc_info=True)
        return {"processed": 0, "succeeded": 0, "requeued": 0, "dead": 0}


async def _retry_async() -> Dict[str, int]:
    from ...config import settings
    from ...agent.retry_queue import dequeue_due_actions, requeue_with_backoff, mark_dead
    from ...agent.tools.base import execute_tool
    import redis.asyncio as aioredis  # same import pattern as server.py and cache.py

    # Stand-alone Redis client — no app.state available in Celery worker
    redis = None
    try:
        redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        items = await dequeue_due_actions(redis, limit=50)

        if not items:
            return {"processed": 0, "succeeded": 0, "requeued": 0, "dead": 0}

        store_client = _build_store_client(settings)
        stats = {"processed": 0, "succeeded": 0, "requeued": 0, "dead": 0}

        for item in items:
            stats["processed"] += 1
            await _retry_one(item, store_client=store_client, redis=redis, stats=stats)

        try:
            await store_client.close()
        except Exception:
            pass

        return stats
    finally:
        if redis is not None:
            try:
                await redis.aclose()
            except Exception:
                pass


async def _retry_one(
    item: Dict[str, Any],
    *,
    store_client,
    redis,
    stats: Dict[str, int],
) -> None:
    from ...agent.retry_queue import requeue_with_backoff, mark_dead
    from ...agent.tools.base import execute_tool

    tool_name: str = item.get("tool_name", "")
    tool_args: dict = item.get("tool_args") or {}
    session_id: str = item.get("session_id", "")
    attempt: int = item.get("attempt", 1)
    max_attempts: int = item.get("max_attempts", 3)

    logger.info(
        "Retrying tool=%s session=%s attempt=%d/%d",
        tool_name, session_id, attempt, max_attempts,
    )

    try:
        execution = await execute_tool(
            tool_name=tool_name,
            tool_args=tool_args,
            session_id=session_id,
            store_client=store_client,
        )
        result = execution.result

        if result.get("success") is False:
            raise RuntimeError(result.get("error") or "tool returned success=False")

        logger.info(
            "Retry succeeded: tool=%s session=%s attempt=%d",
            tool_name, session_id, attempt,
        )
        stats["succeeded"] += 1

    except Exception as exc:
        error_str = str(exc)[:500]
        logger.warning(
            "Retry failed: tool=%s session=%s attempt=%d error=%s",
            tool_name, session_id, attempt, error_str,
        )
        item = dict(item, last_error=error_str)

        if attempt >= max_attempts:
            await mark_dead(redis, item, reason=error_str)
            stats["dead"] += 1
        else:
            await requeue_with_backoff(redis, item)
            stats["requeued"] += 1


def _build_store_client(settings: Any):
    """Build a store client from env-var settings (no app.state in Celery)."""
    platform = settings.platform.lower()
    if platform == "shopify":
        from ...integrations.shopify.client import ShopifyClient
        return ShopifyClient(
            store_domain=settings.shopify_store_domain,
            storefront_token=settings.shopify_storefront_token,
            admin_token=settings.shopify_admin_token,
            api_version=settings.shopify_api_version,
            redis_client=None,
        )
    elif platform == "custom_api":
        from ...integrations.custom_api.client import CustomApiClient
        return CustomApiClient(
            base_url=settings.custom_api_base_url,
            api_key=settings.custom_api_key,
        )
    else:
        from ...integrations.woocommerce.client import WooCommerceClient
        return WooCommerceClient(
            store_url=settings.woocommerce_store_url,
            consumer_key=settings.woocommerce_consumer_key,
            consumer_secret=settings.woocommerce_consumer_secret,
            redis_client=None,
        )
