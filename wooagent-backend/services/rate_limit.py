from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address


def _ip_primary(request):
    """
    Rate-limit key: always anchored to the client IP so that rotating
    session IDs cannot be used to bypass per-client limits.
    """
    return get_remote_address(request)


limiter = Limiter(key_func=_ip_primary)
