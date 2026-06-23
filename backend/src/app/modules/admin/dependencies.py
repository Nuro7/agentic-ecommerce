"""Operator authentication for /api/v1/admin/* endpoints.

A static admin key compared in constant time against the `X-Admin-Token` header.
If `ADMIN_API_KEY` is unset the admin API is DISABLED (503) — a missing key must
never be interpreted as "open".
"""
import hmac

from fastapi import Header, HTTPException, status

from ...config import settings


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    configured = settings.admin_api_key or ""
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin API is disabled (ADMIN_API_KEY not set).",
        )
    if not x_admin_token or not hmac.compare_digest(x_admin_token, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin token.",
        )
