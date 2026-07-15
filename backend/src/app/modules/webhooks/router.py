import base64
import hmac
import hashlib
import json
import logging
from fastapi import APIRouter, Depends, Request, HTTPException, status, Header
from sqlalchemy.ext.asyncio import AsyncSession
from .service import WebhookService
from ...core.database import get_db
from ...config import settings

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = logging.getLogger(__name__)


def _verify_hex_signature(body: bytes, signature: str | None, *, header: str) -> None:
    """Verify a hex-encoded HMAC-SHA256(SHARED_SECRET, body) signature (Woo / custom)."""
    if not signature:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Missing {header}")
    expected = hmac.new(settings.shared_secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid {header}")


def _verify_woocommerce_signature(body: bytes, signature: str | None) -> None:
    _verify_hex_signature(body, signature, header="signature")


def _verify_custom_signature(body: bytes, signature: str | None) -> None:
    _verify_hex_signature(body, signature, header="X-Speako-Signature")


def _verify_shopify_signature(body: bytes, signature: str | None) -> None:
    """Shopify signs webhooks as base64(HMAC-SHA256(api_secret, raw_body))."""
    secret = settings.shopify_api_secret
    if not secret:
        # Fail closed: refuse to ingest unverifiable Shopify webhooks in prod.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Shopify webhook verification not configured (SHOPIFY_API_SECRET unset)",
        )
    if not signature:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing X-Shopify-Hmac-SHA256")
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Shopify signature")


async def _require_tenant(db: AsyncSession, tenant_id: str):
    """Reject webhooks for tenants that don't exist (prevents table bloat/spoofing)."""
    from ...modules.tenants.repository import TenantRepository
    tenant = await TenantRepository(db).get_by_id(tenant_id)
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    return tenant


def _parse_json_body(body: bytes):
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body")


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
    await _require_tenant(db, tenant_id)
    payload = _parse_json_body(body)
    await WebhookService(db).ingest(tenant_id, x_wc_webhook_topic or "unknown", "woocommerce", payload)
    return {"received": True}


@router.post("/shopify/{tenant_id}")
async def shopify_webhook(
    tenant_id: str,
    request: Request,
    x_shopify_topic: str = Header(None),
    x_shopify_hmac_sha256: str = Header(None),
    x_shopify_shop_domain: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    body = await request.body()
    _verify_shopify_signature(body, x_shopify_hmac_sha256)

    # Bind the signed event to a real tenant and confirm the shop domain matches,
    # so a valid signature for shop A cannot be replayed against tenant B.
    from ...modules.tenants.repository import TenantRepository
    tenant = await TenantRepository(db).get_by_id(tenant_id)
    if not tenant or not tenant.shopify_domain:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shopify tenant not found")
    if x_shopify_shop_domain and x_shopify_shop_domain.strip().lower() != tenant.shopify_domain.lower():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Shop domain mismatch")

    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body")
    await WebhookService(db).ingest(tenant_id, x_shopify_topic or "unknown", "shopify", payload)
    return {"received": True}


@router.post("/shopify/compliance")
async def shopify_compliance_webhook(
    request: Request,
    x_shopify_topic: str = Header(None),
    x_shopify_hmac_sha256: str = Header(None),
    x_shopify_shop_domain: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Shopify mandatory GDPR webhooks (customers/data_request, customers/redact,
    shop/redact). Configured as a single URL in the Partner Dashboard, so there is
    no tenant_id in the path — resolve the tenant by shop domain. Always returns
    200 (a Shopify requirement), even for an unknown/already-removed shop."""
    body = await request.body()
    _verify_shopify_signature(body, x_shopify_hmac_sha256)
    payload = _parse_json_body(body)
    topic = x_shopify_topic or "unknown"

    shop_domain = (payload.get("shop_domain") or x_shopify_shop_domain or "").strip().lower()
    from ...modules.tenants.models import Tenant
    from sqlalchemy import select, func
    tenant = None
    if shop_domain:
        res = await db.execute(
            select(Tenant).where(func.lower(Tenant.shopify_domain) == shop_domain)
        )
        tenant = res.scalar_one_or_none()

    if tenant:
        # Reuse the durable ingest → 60s-worker pipeline; the registered compliance
        # handlers do the actual redaction under the tenant's RLS scope.
        await WebhookService(db).ingest(tenant.id, topic, "shopify", payload)
    else:
        logger.info("Compliance webhook for unknown shop: topic=%s shop=%s", topic, shop_domain)
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
    await _require_tenant(db, tenant_id)

    payload = _parse_json_body(body)
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
        await svc.invalidate_search_cache(tenant_id)
        return {"received": True, "deleted": len(items)}

    # Handle product create / update
    items = payload if isinstance(payload, list) else [payload]
    count = 0
    for item in items:
        await svc.upsert_product(tenant_id, item)
        count += 1

    if count:
        await svc.invalidate_search_cache(tenant_id)
    return {"received": True, "upserted": count}
