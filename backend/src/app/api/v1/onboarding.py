"""Self-service merchant onboarding — no admin needed.

POST /api/v1/onboard
  → validates credentials
  → creates tenant in DB
  → queues initial product sync
  → returns tenant_id + ready-to-paste widget snippet

Works for all three platforms: shopify, woocommerce, custom_api.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import settings
from ...core.database import get_db
from ...core.net import validate_public_http_url, UnsafeUrlError
from ...core.security import hash_password
from ...modules.tenants.repository import TenantRepository
from ...modules.billing.models import Subscription
from ...modules.billing.repository import BillingRepository
from ...modules.tenants.schemas import TenantCreate
from ...modules.tenants.service import TenantService
from ...core.exceptions import ConflictError


def _validate_store_urls(platform: str, base_url: Optional[str], store_url: Optional[str]) -> None:
    """Reject tenant-supplied store URLs that resolve to internal addresses (SSRF)."""
    target = base_url if platform == "custom_api" else store_url if platform == "woocommerce" else None
    if not target:
        return
    # Dev only: allow localhost / private store URLs so a local test store can be
    # onboarded. Production still rejects non-public targets (SSRF protection).
    if settings.environment.lower() in ("dev", "development", "local"):
        return
    try:
        validate_public_http_url(target)
    except UnsafeUrlError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Store URL is not allowed: {exc}",
        )

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/onboard", tags=["onboarding"])


# ── Request / response schemas ────────────────────────────────────────────────

class OnboardRequest(BaseModel):
    # Merchant identity
    store_name: str
    email: EmailStr
    plan: str = "free"
    # Optional at signup; required before the merchant can log in to the dashboard.
    password: Optional[str] = None

    # Which platform this store runs on
    platform: str = "custom_api"

    # Shopify credentials (platform=shopify)
    shopify_domain: Optional[str] = None
    shopify_storefront_token: Optional[str] = None
    shopify_access_token: Optional[str] = None

    # WooCommerce credentials (platform=woocommerce)
    woocommerce_store_url: Optional[str] = None
    woocommerce_consumer_key: Optional[str] = None
    woocommerce_consumer_secret: Optional[str] = None

    # Custom API credentials (platform=custom_api)
    custom_api_base_url: Optional[str] = None
    custom_api_key: Optional[str] = None

    @field_validator("platform")
    @classmethod
    def validate_platform(cls, v: str) -> str:
        allowed = {"shopify", "woocommerce", "custom_api"}
        v = v.lower().strip()
        if v not in allowed:
            raise ValueError(f"platform must be one of: {', '.join(sorted(allowed))}")
        return v

    def validate_credentials(self) -> None:
        """Raise ValueError if required credentials for the chosen platform are missing."""
        if self.platform == "shopify":
            missing = [
                f for f in ["shopify_domain", "shopify_storefront_token"]
                if not getattr(self, f)
            ]
            if missing:
                raise ValueError(
                    f"Shopify setup requires: {', '.join(missing)}"
                )
        elif self.platform == "woocommerce":
            missing = [
                f for f in [
                    "woocommerce_store_url",
                    "woocommerce_consumer_key",
                    "woocommerce_consumer_secret",
                ]
                if not getattr(self, f)
            ]
            if missing:
                raise ValueError(
                    f"WooCommerce setup requires: {', '.join(missing)}"
                )
        elif self.platform == "custom_api":
            if not self.custom_api_base_url:
                raise ValueError(
                    "custom_api setup requires: custom_api_base_url"
                )


class OnboardResponse(BaseModel):
    tenant_id: str
    store_name: str
    platform: str
    message: str
    widget_snippet: str
    next_steps: list[str]


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post("/", response_model=OnboardResponse, status_code=201)
async def onboard_merchant(
    data: OnboardRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> OnboardResponse:
    """
    Self-service merchant registration.

    Validates the provided store credentials, creates a tenant record,
    queues the first product sync, and returns a ready-to-paste widget
    snippet.  No admin intervention required.
    """
    # Validate platform-specific credentials
    try:
        data.validate_credentials()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )

    # SSRF guard: reject store URLs pointing at internal addresses.
    _validate_store_urls(data.platform, data.custom_api_base_url, data.woocommerce_store_url)

    # Create tenant (raises ConflictError if email already registered)
    tenant_data = TenantCreate(
        name=data.store_name,
        email=data.email,
        plan=data.plan,
        platform=data.platform,
        shopify_domain=data.shopify_domain,
        shopify_storefront_token=data.shopify_storefront_token,
        shopify_access_token=data.shopify_access_token,
        woocommerce_store_url=data.woocommerce_store_url,
        woocommerce_consumer_key=data.woocommerce_consumer_key,
        woocommerce_consumer_secret=data.woocommerce_consumer_secret,
        custom_api_base_url=data.custom_api_base_url,
        custom_api_key=data.custom_api_key,
    )

    try:
        tenant = await TenantService(db).create_tenant(tenant_data)
    except ConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A merchant account with this email already exists.",
        )

    # Store the login password (argon2). Without this the merchant cannot log in.
    if data.password:
        tenant.hashed_password = hash_password(data.password)
        await db.commit()

    # Auto-enrol new merchant on the Starter plan (free, 200 credits/month).
    try:
        billing_repo = BillingRepository(db)
        starter_plan = await billing_repo.get_plan_by_name("starter")
        if starter_plan:
            now = datetime.now(timezone.utc)
            sub = Subscription(
                id=str(uuid.uuid4()),
                tenant_id=tenant.id,
                plan_id=starter_plan.id,
                status="active",
                current_period_start=now,
                current_period_end=now + timedelta(days=30),
            )
            db.add(sub)
            await db.commit()
            logger.info("Starter subscription created for tenant=%s", tenant.id)
        else:
            logger.warning(
                "Starter plan row not found — run migration 0009 first. "
                "Tenant %s has no subscription.", tenant.id,
            )
    except Exception as exc:
        logger.warning(
            "Could not create subscription for tenant=%s: %s", tenant.id, exc
        )

    # Queue initial product sync (non-blocking — Celery)
    try:
        from ...workers.tasks.sync_products import sync_products
        sync_products.delay(tenant_id=tenant.id)
        logger.info("Initial product sync queued for tenant=%s", tenant.id)
    except Exception as exc:
        logger.warning(
            "Could not queue product sync for tenant=%s (Celery unavailable): %s",
            tenant.id, exc,
        )

    # Build widget snippet
    backend_url = str(request.base_url).rstrip("/")
    snippet = _widget_snippet(
        backend_url=backend_url,
        tenant_id=tenant.id,
        store_name=data.store_name,
    )

    next_steps = _next_steps(data.platform, backend_url)

    logger.info(
        "Merchant onboarded: tenant=%s platform=%s store=%s",
        tenant.id, data.platform, data.store_name,
    )

    return OnboardResponse(
        tenant_id=tenant.id,
        store_name=data.store_name,
        platform=data.platform,
        message=(
            f"Welcome to Speako! Your store '{data.store_name}' is registered. "
            f"Products are being synced in the background."
        ),
        widget_snippet=snippet,
        next_steps=next_steps,
    )


# ── Tenant lookup by API key (used by speako-loader.js on 409) ───────────────

@router.get("/lookup")
async def lookup_tenant_by_api_key(
    api_key: str = Query(..., description="custom_api_key set during onboarding"),
    db: AsyncSession = Depends(get_db),
):
    """
    Return tenant_id for a given custom_api_key.
    Called by speako-loader.js when POST /onboard/ returns 409 (already registered).
    """
    tenant = await TenantRepository(db).get_by_custom_api_key(api_key)
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    return {"tenant_id": tenant.id, "store_name": tenant.name}


# ── Quick connectivity test ───────────────────────────────────────────────────

class TestConnectionRequest(BaseModel):
    platform: str
    shopify_domain: Optional[str] = None
    shopify_storefront_token: Optional[str] = None
    woocommerce_store_url: Optional[str] = None
    woocommerce_consumer_key: Optional[str] = None
    woocommerce_consumer_secret: Optional[str] = None
    custom_api_base_url: Optional[str] = None
    custom_api_key: Optional[str] = None


class TestConnectionResponse(BaseModel):
    ok: bool
    platform: str
    products_found: int
    message: str


@router.post("/test-connection", response_model=TestConnectionResponse)
async def test_store_connection(data: TestConnectionRequest) -> TestConnectionResponse:
    """
    Test store credentials before registering.

    Calls search_products(query='', limit=1) on the provided credentials
    and reports whether the connection succeeded.  Use this to validate
    credentials before submitting the full /onboard request.
    """
    platform = data.platform.lower()
    # SSRF guard: validate caller-supplied URLs before we make any request to them.
    _validate_store_urls(platform, data.custom_api_base_url, data.woocommerce_store_url)
    store_client = None
    try:
        if platform == "shopify":
            from ...integrations.shopify.client import ShopifyClient
            store_client = ShopifyClient(
                store_domain=data.shopify_domain or "",
                storefront_token=data.shopify_storefront_token or "",
                admin_token="",
            )
        elif platform == "woocommerce":
            from ...integrations.woocommerce.client import WooCommerceClient
            store_client = WooCommerceClient(
                store_url=data.woocommerce_store_url or "",
                consumer_key=data.woocommerce_consumer_key or "",
                consumer_secret=data.woocommerce_consumer_secret or "",
            )
        elif platform == "custom_api":
            from ...integrations.custom_api.client import CustomApiClient
            store_client = CustomApiClient(
                base_url=data.custom_api_base_url or "",
                api_key=data.custom_api_key or "",
            )
        else:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown platform: {platform}",
            )

        products = await store_client.search_products(
            query="", limit=1, in_stock_only=False,
        )
        return TestConnectionResponse(
            ok=True,
            platform=platform,
            products_found=len(products),
            message=f"Connection successful. Found {len(products)} product(s).",
        )

    except HTTPException:
        raise
    except Exception as exc:
        return TestConnectionResponse(
            ok=False,
            platform=platform,
            products_found=0,
            message=f"Connection failed: {exc}",
        )
    finally:
        if store_client is not None:
            try:
                await store_client.close()
            except Exception:
                pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def _widget_snippet(backend_url: str, tenant_id: str, store_name: str) -> str:
    return (
        f'<!-- Speako AI Shopping Assistant -->\n'
        f'<script>\n'
        f'  window.wooagent_config = {{\n'
        f'    backend_url: "{backend_url}",\n'
        f'    tenant_id:   "{tenant_id}",\n'
        f'    store_name:  "{store_name}"\n'
        f'  }};\n'
        f'</script>\n'
        f'<script src="{backend_url}/static/wooagent-widget.js" async></script>'
    )


def _next_steps(platform: str, backend_url: str) -> list[str]:
    steps = [
        "1. Paste the widget_snippet into your store's HTML, just before </body>.",
        "2. Your products are syncing in the background — this takes 1–5 minutes.",
        "3. Refresh your store page and look for the Aria chat bubble.",
    ]
    if platform == "custom_api":
        steps.insert(
            1,
            "1b. Make sure your store's API endpoints are live and return JSON "
            "(GET /products/search, POST /cart/add, etc.).",
        )
    elif platform == "shopify":
        steps.insert(
            1,
            "1b. For Shopify, you can also register via the Shopify App Store "
            f"install URL: {backend_url}/api/v1/shopify/install",
        )
    return steps
