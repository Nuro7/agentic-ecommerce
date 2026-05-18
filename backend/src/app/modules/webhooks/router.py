import hmac
import hashlib
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
    import json
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
    import json
    payload = json.loads(body)
    await WebhookService(db).ingest(tenant_id, x_shopify_topic or "unknown", "shopify", payload)
    return {"received": True}
