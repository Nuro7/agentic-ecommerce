from __future__ import annotations

import hashlib
import hmac
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
from fastapi import HTTPException, Request

from ..config import settings

_ph = PasswordHasher()


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, plain)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=settings.jwt_expire_minutes))
    return jwt.encode({**data, "exp": expire}, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise ValueError("Invalid token") from exc


# ── Widget request security (migrated from wooagent-backend) ─────────────────

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
    """HMAC-SHA256 over timestamp.path.body — path prevents cross-endpoint replay."""
    payload = f"{timestamp}.{path}.".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


async def verify_hmac(request: Request, body: bytes, *, required: bool = True) -> None:
    shared_secret = settings.shared_secret
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

    expected = compute_signature(shared_secret, timestamp, body, request.url.path)
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid request signature")


def allowed_origin(origin: Optional[str]) -> bool:
    allowed_raw = settings.shared_secret  # reuse ALLOWED_ORIGINS if added to settings
    if not allowed_raw:
        return True
    allowed = [item.strip() for item in allowed_raw.split(",") if item.strip()]
    if not origin:
        return False
    return origin in allowed
