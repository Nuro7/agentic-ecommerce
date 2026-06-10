"""Application-level encryption for secret DB columns (store credentials).

EncryptedText is a SQLAlchemy TypeDecorator that encrypts on write and decrypts
on read using Fernet (AES-128-CBC + HMAC) keyed by settings.encryption_key.

Designed to be SAFE TO DEPLOY incrementally:
  • No ENCRYPTION_KEY set (or `cryptography` missing) → pass-through plaintext,
    so existing behaviour is unchanged until the key is provisioned.
  • Reading a legacy plaintext value (no `enc:v1:` prefix) → returned as-is, so
    rows written before encryption keep working; they get encrypted on next write.

Generate a key once:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Optional

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from ..config import settings

logger = logging.getLogger(__name__)

_PREFIX = "enc:v1:"


@lru_cache(maxsize=1)
def _fernet():
    """Return a Fernet instance, or None if no/invalid key or lib unavailable."""
    key = (settings.encryption_key or "").strip()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode())
    except Exception as exc:  # missing lib or malformed key
        logger.warning("ENCRYPTION_KEY set but unusable (%s) — credentials stored in plaintext", exc)
        return None


class EncryptedText(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Optional[str], dialect) -> Optional[str]:
        if value is None:
            return None
        f = _fernet()
        if f is None:
            return value  # not configured → store plaintext (no behaviour change)
        if value.startswith(_PREFIX):
            return value  # already encrypted (idempotent)
        return _PREFIX + f.encrypt(value.encode()).decode()

    def process_result_value(self, value: Optional[str], dialect) -> Optional[str]:
        if value is None:
            return None
        if not value.startswith(_PREFIX):
            return value  # legacy plaintext — back-compat
        f = _fernet()
        if f is None:
            return value
        try:
            return f.decrypt(value[len(_PREFIX):].encode()).decode()
        except Exception as exc:
            logger.error("Failed to decrypt credential column (%s)", exc)
            return value
