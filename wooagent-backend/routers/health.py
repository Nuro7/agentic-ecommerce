from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health")
async def health(request: Request):
    redis_status = "connected" if getattr(request.app.state, "redis", None) is not None else "disconnected"
    woo_status = "connected" if getattr(request.app.state, "woo_client", None) is not None else "disconnected"

    return {
        "status": "ok",
        "redis": redis_status,
        "woocommerce": woo_status,
        "version": "1.0.0",
    }
