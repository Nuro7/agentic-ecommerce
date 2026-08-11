import uuid
import time
import urllib.parse
import logging
from fastapi import Request
from fastapi.responses import JSONResponse
from .exceptions import AppError

logger = logging.getLogger(__name__)


async def request_id_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = round((time.perf_counter() - start) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    logger.info("%s %s %s %.0fms", request.method, request.url.path, response.status_code, elapsed)
    return response


async def app_error_handler(request: Request, exc: AppError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


# Multi-tenant CORS origin matcher. Every Shopify storefront (https://*.myshopify.com)
# is allowed automatically so a brand-new store works the moment it installs — no
# per-store env edits. Dashboard/admin origins come from CORS_ORIGINS (exact
# https://... or wildcard https://*.suffix). Credentials stay OFF, so echoing the
# origin never leaks cookies/headers.
def origin_matcher(allowed_origins) -> callable:
    exact = set()
    suffixes = [".myshopify.com"]  # built-in: all Shopify storefronts
    allow_all = False
    for o in allowed_origins or ["*"]:
        o = str(o).strip().rstrip("/")
        if not o:
            continue
        if o == "*":
            allow_all = True
        elif "*." in o:
            # Wildcard suffix — accepts both bare "*.example.com" and
            # "https://*.example.com" forms.
            suffixes.append("." + o.split("*.", 1)[1])
        else:
            exact.add(o)

    def allowed(origin: str) -> bool:
        if not origin or allow_all:
            return True
        if origin in exact:
            return True
        try:
            host = urllib.parse.urlparse(origin).netloc.lower()
        except Exception:
            host = (origin or "").lower()
        return any(host.endswith(s) and len(host) > len(s) for s in suffixes)

    return allowed
