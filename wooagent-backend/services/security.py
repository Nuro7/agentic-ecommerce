from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from typing import Optional

from fastapi import HTTPException, Request


def sanitize_text(value: str, *, max_len: int = 1000) -> str:
    cleaned = re.sub(r"\s+", " ", (value or "").strip())
    return cleaned[:max_len]


def mask_email(value: str) -> str:
    if "@" not in value:
        return ""
    name, domain = value.split("@", 1)
    if len(name) <= 2:
        return f"{name[:1]}*@{domain}"
    return f"{name[:2]}{'*' * (len(name) - 2)}@{domain}"


def compute_signature(secret: str, timestamp: str, body: bytes, path: str = "") -> str:
    """
    HMAC-SHA256 over  timestamp + "." + path + "." + body.
    Including the request path prevents a valid /chat payload from being
    replayed against a different endpoint.
    """
    payload = f"{timestamp}.{path}.".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


async def verify_hmac(request: Request, body: bytes, *, required: bool = True) -> None:
    shared_secret = os.getenv("SHARED_SECRET", "")
    if not shared_secret:
        return

    timestamp = request.headers.get("x-wooagent-timestamp")
    signature = request.headers.get("x-wooagent-signature")

    if not timestamp or not signature:
        if required:
            raise HTTPException(status_code=401, detail="Missing request signature")
        return

    try:
        if abs(int(time.time()) - int(timestamp)) > 300:
            raise HTTPException(status_code=401, detail="Signature timestamp expired")
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid signature timestamp") from exc

    path = request.url.path
    expected = compute_signature(shared_secret, timestamp, body, path)
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid request signature")


def allowed_origin(origin: Optional[str]) -> bool:
    allowed_raw = os.getenv("ALLOWED_ORIGINS", "")
    if not allowed_raw:
        return True

    allowed = [item.strip() for item in allowed_raw.split(",") if item.strip()]
    if not origin:
        return False
    return origin in allowed
