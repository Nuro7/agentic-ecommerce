import hmac
import hashlib
import json
from fastapi import APIRouter, Depends, Request, HTTPException, status, Header
from sqlalchemy.ext.asyncio import AsyncSession
from .service import WebhookService
from ...core.database import get_db
from ...config import settings

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _verify_woocommerce_signature(body: bytes, signature: str | None) -> None:
    if not signature:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing signature")
    expected = hmac.new(settings.shared_secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")


def _verify_custom_signature(body: bytes, signature: str | None) -> None:
    if not signature:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing X-Speako-Signature")
    expected = hmac.new(settings.shared_secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid X-Speako-Signature")


@router.post("/woocommerce/{tenant_id}")
async def woocommerce_webhook(
    tenant_id: str,
    request: Request,
    x_wc_webhook_topic: str = Header(None),
    x_wc_webhook_signature: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    body = await request.body()
    _verify_woocommerce_signature(body, x_wc_webhook_signature)
    payload = json.loads(body)
    await WebhookService(db).ingest(tenant_id, x_wc_webhook_topic or "unknown", "woocommerce", payload)
    return {"received": True}


@router.post("/shopify/{tenant_id}")
async def shopify_webhook(
    tenant_id: str,
    request: Request,
    x_shopify_topic: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    body = await request.body()
    payload = json.loads(body)
    await WebhookService(db).ingest(tenant_id, x_shopify_topic or "unknown", "shopify", payload)
    return {"received": True}


@router.post("/custom/{tenant_id}")
async def custom_platform_webhook(
    tenant_id: str,
    request: Request,
    x_speako_topic: str = Header(default="product.updated"),
    x_speako_signature: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    """
    Webhook receiver for custom e-commerce platforms.

    Accepts a single product object or a list of products.
    HMAC-SHA256 signed with SHARED_SECRET via X-Speako-Signature header.

    Products are immediately upserted into product_cache so Aria can
    search them via hybrid BM25 + vector search.

    Signing example (merchant side):
        sig = hmac.new(shared_secret.encode(), body_bytes, hashlib.sha256).hexdigest()
        headers["X-Speako-Signature"] = sig
    """
    body = await request.body()
    _verify_custom_signature(body, x_speako_signature)

    payload = json.loads(body)
    svc = WebhookService(db)
    topic = x_speako_topic.lower()

    # Handle product deletion
    if topic == "product.deleted":
        items = payload if isinstance(payload, list) else [payload]
        from sqlalchemy import text as sqla_text
        for item in items:
            pid = str(item.get("id") or item.get("platform_id") or "").strip()
            if pid:
                await svc.db.execute(
                    sqla_text("DELETE FROM product_cache WHERE tenant_id = :tid AND platform_id = :pid"),
                    {"tid": tenant_id, "pid": pid},
                )
        await svc.db.commit()
        return {"received": True, "deleted": len(items)}

    # Handle product create / update
    items = payload if isinstance(payload, list) else [payload]
    count = 0
    for item in items:
        await svc.upsert_product(tenant_id, item)
        count += 1

    return {"received": True, "upserted": count}
