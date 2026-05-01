"""RequestID middleware — assigns a unique ID to every request."""

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp

from app.core.context import request_id_var


class RequestIdMiddleware(BaseHTTPMiddleware):
    HEADER = "x-request-id"

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get(self.HEADER) or f"req_{uuid.uuid4().hex[:12]}"
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
            response.headers[self.HEADER] = request_id
            return response
        finally:
            request_id_var.reset(token)
