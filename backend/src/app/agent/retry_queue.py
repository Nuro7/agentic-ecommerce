"""Failed-action retry queue — Phase 10.

When a mutation tool call (add_to_cart, apply_coupon, etc.) fails due to a
transient store-API error, this module enqueues it in Redis for Celery to
retry with exponential back-off.

Redis key schema
----------------
  speako:retry_queue   ZSET  score=next_retry_at (Unix float), member=JSON item
  speako:retry_dead    ZSET  score=failed_at (Unix float),     member=JSON item

Item shape
----------
  {
    "id":          str,       # UUID
    "session_id":  str,
    "tenant_id":   str,
    "tool_name":   str,       # e.g. "add_to_cart"
    "tool_args":   dict,
    "attempt":     int,       # 1-based (current attempt that failed)
    "max_attempts": int,      # default 3
    "created_at":  float,     # Unix timestamp
    "last_error":  str,
  }

Back-off schedule
-----------------
  After attempt 1 → retry in 30 s
  After attempt 2 → retry in 5 min (300 s)
  After attempt 3 → dead-letter (no more retries)

Dead-letter items expire after 7 days.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_QUEUE_KEY = "speako:retry_queue"
_DEAD_KEY = "speako:retry_dead"
_DEAD_TTL_SECS = 7 * 24 * 3600  # 7 days

# Back-off delays indexed by attempt number (1-based).
_BACKOFF: Dict[int, float] = {1: 30.0, 2: 300.0}

# Only mutation tools are worth retrying; read-only tools are idempotent.
RETRYABLE_TOOLS: frozenset[str] = frozenset({
    "add_to_cart",
    "remove_from_cart",
    "update_cart_quantity",
    "apply_coupon",
    "submit_review",
})


# ── Public API ────────────────────────────────────────────────────────────────

async def enqueue_failed_action(
    redis,
    *,
    session_id: str,
    tenant_id: str,
    tool_name: str,
    tool_args: Dict[str, Any],
    error: str,
    max_attempts: int = 3,
) -> None:
    """Push a failed tool call onto the retry queue.

    Safe to call even when redis is None — silently skips in that case.
    Only enqueues tools in RETRYABLE_TOOLS.
    """
    if redis is None:
        return
    if tool_name not in RETRYABLE_TOOLS:
        return

    item: Dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "session_id": session_id,
        "tenant_id": tenant_id,
        "tool_name": tool_name,
        "tool_args": tool_args,
        "attempt": 1,
        "max_attempts": max_attempts,
        "created_at": time.time(),
        "last_error": str(error)[:500],
    }
    next_retry = time.time() + _BACKOFF.get(1, 30.0)
    try:
        await redis.zadd(_QUEUE_KEY, {json.dumps(item): next_retry})
        logger.info(
            "Enqueued failed action tool=%s session=%s retry_in=%.0fs",
            tool_name, session_id, _BACKOFF.get(1, 30.0),
        )
    except Exception as exc:
        logger.warning("Failed to enqueue retry action: %s", exc)


async def dequeue_due_actions(redis, *, limit: int = 50) -> List[Dict[str, Any]]:
    """Pop up to `limit` items whose next_retry_at ≤ now from the queue.

    Fetch-then-remove: reads up to `limit` due members, then removes exactly
    those members (not all due members) so nothing beyond `limit` is lost.
    Returns a list of parsed item dicts.
    """
    if redis is None:
        return []

    now = time.time()
    try:
        # Step 1: fetch up to `limit` due members (raw JSON strings)
        raw_items: list = await redis.zrangebyscore(
            _QUEUE_KEY, 0, now, start=0, num=limit
        )
        if not raw_items:
            return []

        # Step 2: remove only the fetched members — not all due items
        await redis.zrem(_QUEUE_KEY, *raw_items)

        items: List[Dict[str, Any]] = []
        for raw in raw_items:
            try:
                items.append(json.loads(raw))
            except Exception:
                logger.debug("Skipping malformed retry item: %.80s", raw)
        return items
    except Exception as exc:
        logger.warning("dequeue_due_actions failed: %s", exc)
        return []


async def requeue_with_backoff(redis, item: Dict[str, Any]) -> None:
    """Increment the attempt counter and re-add to the queue with back-off.

    If max_attempts is exceeded, moves the item to the dead-letter set instead.
    """
    if redis is None:
        return

    attempt = item.get("attempt", 1) + 1
    max_attempts = item.get("max_attempts", 3)

    if attempt > max_attempts:
        await _move_to_dead(redis, item)
        return

    updated = dict(item, attempt=attempt, last_error=item.get("last_error", ""))
    delay = _BACKOFF.get(attempt, 300.0)
    next_retry = time.time() + delay

    try:
        await redis.zadd(_QUEUE_KEY, {json.dumps(updated): next_retry})
        logger.info(
            "Requeued tool=%s attempt=%d/%d retry_in=%.0fs",
            item.get("tool_name"), attempt, max_attempts, delay,
        )
    except Exception as exc:
        logger.warning("requeue_with_backoff failed: %s", exc)


async def mark_dead(redis, item: Dict[str, Any], *, reason: str = "") -> None:
    """Permanently move item to the dead-letter set."""
    if reason:
        item = dict(item, last_error=reason[:500])
    await _move_to_dead(redis, item)


# ── Internal ──────────────────────────────────────────────────────────────────

async def _move_to_dead(redis, item: Dict[str, Any]) -> None:
    score = time.time()
    try:
        await redis.zadd(_DEAD_KEY, {json.dumps(item): score})
        # Trim dead-letter to last 500 entries to avoid unbounded growth
        await redis.zremrangebyrank(_DEAD_KEY, 0, -501)
        logger.warning(
            "Dead-lettered tool=%s session=%s error=%.80s",
            item.get("tool_name"), item.get("session_id"), item.get("last_error", ""),
        )
    except Exception as exc:
        logger.warning("_move_to_dead failed: %s", exc)
