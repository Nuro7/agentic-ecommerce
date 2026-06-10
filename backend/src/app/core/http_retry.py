"""Transient-failure retry for outbound store-API HTTP calls.

Store APIs (Shopify, WooCommerce, custom) intermittently return 429/5xx or time
out. Without retries a single transient blip surfaces as a user-visible failure
(empty search, failed add-to-cart). request_with_retries() retries idempotent
transient failures with exponential backoff, honoring Retry-After.

Pass a zero-arg coroutine factory that performs ONE request; it is re-invoked
per attempt. The caller still does raise_for_status()/json() on the result.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_DELAY = 10.0


def _backoff(attempt: int, base_delay: float) -> float:
    return min(base_delay * (2 ** (attempt - 1)), _MAX_DELAY)


async def request_with_retries(send, *, attempts: int = 3, base_delay: float = 0.4, label: str = "http"):
    """Call send() with retries on transient failures; return the httpx.Response."""
    resp = None
    for attempt in range(1, attempts + 1):
        try:
            resp = await send()
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt >= attempts:
                raise
            delay = _backoff(attempt, base_delay)
            logger.warning("%s transient error (%s) — retry %d/%d in %.1fs",
                           label, type(exc).__name__, attempt, attempts, delay)
            await asyncio.sleep(delay)
            continue

        if resp.status_code in _RETRY_STATUSES and attempt < attempts:
            retry_after = resp.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else _backoff(attempt, base_delay)
            except (TypeError, ValueError):
                delay = _backoff(attempt, base_delay)
            delay = min(delay, _MAX_DELAY)
            logger.warning("%s HTTP %d — retry %d/%d in %.1fs",
                           label, resp.status_code, attempt, attempts, delay)
            await asyncio.sleep(delay)
            continue

        return resp

    return resp  # last response (still has the transient status) — caller decides
