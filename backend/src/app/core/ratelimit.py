"""Redis fixed-window rate limiting for the public widget plane.

The widget endpoints (/greet, /cart, voice WS) are unauthenticated and each call
can trigger LLM/TTS spend or a live store-API call. Without a limit, a buggy
client loop or an attacker can drive unbounded cost and exhaust the DB pool.

Limits are per (tenant, client-IP) so one abusive shopper can't affect others,
and one noisy store can't exhaust a global bucket. Fails OPEN when Redis is
unavailable — availability is preferred over hard-blocking real shoppers.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)


async def _hit(redis, key: str, limit: int, window: int) -> bool:
    """Increment a fixed-window counter. True = within limit, False = exceeded."""
    if redis is None:
        return True  # fail-open: no Redis → don't block shoppers
    try:
        count = await redis.incr(key)
        # Set the TTL via NX every call: NX won't extend an existing window, but
        # it REPAIRS a key that was incremented without a TTL (e.g. a prior EXPIRE
        # failed) — otherwise that bucket would count forever and 429 permanently.
        await redis.expire(key, window, nx=True)
        return count <= limit
    except Exception as exc:  # Redis hiccup must never take down the widget
        logger.warning("rate-limit check failed (%s) — allowing request", exc)
        return True


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _tenant_key(request: Request) -> str:
    shop = request.query_params.get("shop", "").strip()
    if shop:
        return f"shop:{shop}"
    tid = request.headers.get("X-Tenant-ID", "").strip()
    if tid:
        return f"tid:{tid}"
    return "anon"


async def check_rate_limit(
    redis, *, tenant_key: str, ip: str, limit: int, window: int = 60, scope: str = "ws"
) -> bool:
    """Raw check for non-HTTP callers (e.g. the voice WebSocket handler)."""
    return await _hit(redis, f"rl:{scope}:{tenant_key}:{ip}", limit, window)


def rate_limit(*, limit: int, window: int = 60, scope: str = "widget"):
    """FastAPI dependency factory: per (tenant, IP) fixed-window limit → 429."""

    async def _dep(request: Request) -> None:
        redis = getattr(request.app.state, "redis", None)
        key = f"rl:{scope}:{_tenant_key(request)}:{_client_ip(request)}"
        if not await _hit(redis, key, limit, window):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests — please slow down.",
                headers={"Retry-After": str(window)},
            )

    return _dep
