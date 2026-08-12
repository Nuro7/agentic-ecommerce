"""Overlay REST API — server-side proxy for the fullscreen shopping overlay.

The overlay SPA (backend/static/speako-overlay.js) talks ONLY to these routes.
The Shopify Storefront token never reaches the browser: every GraphQL call is
made server-side by :class:`StorefrontService`, using per-tenant credentials
(resolved from the ``shop`` query param) or the global env fallback (dev mode).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ...core.database import AsyncSessionLocal
from ...integrations.shopify.storefront import StorefrontService, StorefrontServiceError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/overlay", tags=["overlay"])


# ── Request bodies ────────────────────────────────────────────────────────────

class CartLineIn(BaseModel):
    merchandise_id: str = Field(..., description="ProductVariant GID")
    quantity: int = Field(1, ge=1, le=999)


class DeliveryAddressIn(BaseModel):
    address1: str = ""
    address2: str = ""
    city: str = ""
    country_code: str = ""
    province_code: str = ""
    zip: str = ""
    first_name: str = ""
    last_name: str = ""
    phone: str = ""


class CartCreateIn(BaseModel):
    shop: Optional[str] = None
    lines: List[CartLineIn] = []


class CartLinesAddIn(BaseModel):
    shop: Optional[str] = None
    cart_id: str
    lines: List[CartLineIn]


class CartDiscountIn(BaseModel):
    shop: Optional[str] = None
    cart_id: str
    codes: List[str] = []


class CartBuyerIn(BaseModel):
    shop: Optional[str] = None
    cart_id: str
    email: Optional[str] = None
    phone: Optional[str] = None
    country_code: Optional[str] = None
    delivery_address: Optional[DeliveryAddressIn] = None


class CartCheckoutIn(BaseModel):
    shop: Optional[str] = None
    cart_id: str
    email: Optional[str] = None
    discount_codes: List[str] = []
    delivery_address: Optional[DeliveryAddressIn] = None


# ── Dependency: resolve StorefrontService (overridable in tests) ─────────────

async def resolve_storefront(request: Request) -> StorefrontService:
    """Build the per-tenant Storefront service. Token stays server-side."""
    shop = (
        request.query_params.get("shop")
        or request.headers.get("x-shopify-domain")
        or ""
    )
    domain = token = ""
    if shop:
        try:
            from ...modules.tenants.models import Tenant
            from sqlalchemy import select

            async with AsyncSessionLocal() as db:
                result = await db.execute(select(Tenant).where(Tenant.shopify_domain == shop))
                tenant = result.scalar_one_or_none()
                if tenant:
                    domain = tenant.shopify_domain or ""
                    token = tenant.shopify_storefront_token or ""
        except Exception as exc:
            logger.debug("overlay tenant lookup failed for shop=%s: %s", shop, exc)

    service = StorefrontService(
        store_domain=domain or os.getenv("SHOPIFY_STORE_DOMAIN", ""),
        storefront_token=token or os.getenv("SHOPIFY_STOREFRONT_TOKEN", ""),
    )
    return service


def _client_ip(request: Request) -> Optional[str]:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def _cart_lines(payload: List[CartLineIn]) -> List[Dict[str, Any]]:
    return [{"merchandiseId": l.merchandise_id, "quantity": l.quantity} for l in payload]


def _address_input(addr: DeliveryAddressIn) -> Dict[str, Any]:
    return {
        "address1": addr.address1,
        "address2": addr.address2,
        "city": addr.city,
        "countryCode": addr.country_code,
        "provinceCode": addr.province_code,
        "zip": addr.zip,
        "firstName": addr.first_name,
        "lastName": addr.last_name,
        "phone": addr.phone,
    }


def _selectable_address(addr: DeliveryAddressIn) -> Dict[str, Any]:
    """Wrap a delivery address in the modern ``CartSelectableAddressInput`` shape.

    Shopify's hosted checkout only pre-fills the delivery form when the address
    is sent as ``{address: {deliveryAddress: {...}}, selected: true}``. The
    legacy top-level ``{deliveryAddress: {...}}`` shape returns a checkoutUrl but
    leaves the form blank, so we always send ``selected: true`` here.
    """
    return {
        "address": {"deliveryAddress": _address_input(addr)},
        "selected": True,
        "oneTimeUse": True,
    }


def _err(status: int, message: str, detail: Any = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"errors": [{"message": message}], "detail": detail})


async def _run(sf: StorefrontService, coro, request: Request):
    """Execute a Storefront call, mapping failures to clean HTTP responses."""
    try:
        return await coro
    except StorefrontServiceError as exc:
        logger.warning("overlay storefront error: %s", exc)
        return _err(502, "Storefront request failed: %s" % exc)
    except httpx.HTTPError as exc:
        logger.warning("overlay storefront http error: %s", exc)
        return _err(502, "Could not reach the store's catalog")
    except Exception as exc:  # defensive — never crash the overlay chain
        logger.warning("overlay unexpected error: %s", exc)
        return _err(500, "Unexpected overlay error")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/search")
async def overlay_search(
    q: str = Query("", description="Search query"),
    first: int = Query(20, ge=1, le=50),
    shop: Optional[str] = None,
    request: Request = None,
    sf: StorefrontService = Depends(resolve_storefront),
) -> Any:
    if not sf.is_configured:
        return _err(400, "Store not configured for overlay")
    return await _run(
        sf,
        sf.search_products(q or "", first=first, buyer_ip=_client_ip(request)),
        request,
    )


@router.get("/product/{handle}")
async def overlay_product(
    handle: str,
    shop: Optional[str] = None,
    request: Request = None,
    sf: StorefrontService = Depends(resolve_storefront),
) -> Any:
    if not sf.is_configured:
        return _err(400, "Store not configured for overlay")
    product = await _run(
        sf, sf.get_product(handle, buyer_ip=_client_ip(request)), request
    )
    if isinstance(product, JSONResponse):
        return product
    if not product:
        return _err(404, "Product not found")
    return product


@router.post("/cart")
async def overlay_cart_create(
    payload: CartCreateIn,
    request: Request,
    sf: StorefrontService = Depends(resolve_storefront),
) -> Any:
    if not sf.is_configured:
        return _err(400, "Store not configured for overlay")
    return await _run(
        sf,
        sf.cart_create(lines=_cart_lines(payload.lines), buyer_ip=_client_ip(request)),
        request,
    )


@router.post("/cart/lines")
async def overlay_cart_lines_add(
    payload: CartLinesAddIn,
    request: Request,
    sf: StorefrontService = Depends(resolve_storefront),
) -> Any:
    if not sf.is_configured:
        return _err(400, "Store not configured for overlay")
    return await _run(
        sf,
        sf.cart_lines_add(payload.cart_id, _cart_lines(payload.lines), buyer_ip=_client_ip(request)),
        request,
    )


@router.post("/cart/discount")
async def overlay_cart_discount(
    payload: CartDiscountIn,
    request: Request,
    sf: StorefrontService = Depends(resolve_storefront),
) -> Any:
    if not sf.is_configured:
        return _err(400, "Store not configured for overlay")
    return await _run(
        sf,
        sf.cart_discount_codes_update(payload.cart_id, payload.codes, buyer_ip=_client_ip(request)),
        request,
    )


@router.post("/cart/buyer")
async def overlay_cart_buyer(
    payload: CartBuyerIn,
    request: Request,
    sf: StorefrontService = Depends(resolve_storefront),
) -> Any:
    if not sf.is_configured:
        return _err(400, "Store not configured for overlay")
    buyer_identity: Dict[str, Any] = {}
    if payload.email:
        buyer_identity["email"] = payload.email
    if payload.phone:
        buyer_identity["phone"] = payload.phone
    if payload.country_code:
        buyer_identity["countryCode"] = payload.country_code
    result = await _run(
        sf,
        sf.cart_buyer_identity_update(payload.cart_id, buyer_identity, buyer_ip=_client_ip(request)),
        request,
    )
    if isinstance(result, JSONResponse):
        return result
    if payload.delivery_address:
        address_result = await _run(
            sf,
            sf.cart_delivery_addresses_replace(
                payload.cart_id,
                [_selectable_address(payload.delivery_address)],
                buyer_ip=_client_ip(request),
            ),
            request,
        )
        if isinstance(address_result, JSONResponse):
            return address_result
        if address_result.get("errors"):
            return JSONResponse(
                status_code=422,
                content={"errors": address_result["errors"], "detail": None},
            )
    return result


@router.post("/cart/checkout")
async def overlay_cart_checkout(
    payload: CartCheckoutIn,
    request: Request,
    sf: StorefrontService = Depends(resolve_storefront),
) -> Any:
    """Bind buyer identity + delivery address + discount codes, then return the
    checkout URL. This is the ONLY transition the overlay is allowed to hard-
    navigate to — mic/WebSocket stay alive until this redirect."""
    if not sf.is_configured:
        return _err(400, "Store not configured for overlay")
    ip = _client_ip(request)

    buyer_identity: Dict[str, Any] = {}
    if payload.email:
        buyer_identity["email"] = payload.email
    result = await _run(
        sf,
        sf.cart_buyer_identity_update(payload.cart_id, buyer_identity, buyer_ip=ip),
        request,
    )
    if isinstance(result, JSONResponse):
        return result
    if result.get("errors"):
        return JSONResponse(status_code=422, content={"errors": result["errors"], "detail": None})

    if payload.delivery_address:
        address_result = await _run(
            sf,
            sf.cart_delivery_addresses_replace(
                payload.cart_id,
                [_selectable_address(payload.delivery_address)],
                buyer_ip=ip,
            ),
            request,
        )
        if isinstance(address_result, JSONResponse):
            return address_result
        if address_result.get("errors"):
            return JSONResponse(status_code=422, content={"errors": address_result["errors"], "detail": None})
        result = address_result

    if payload.discount_codes:
        discount_result = await _run(
            sf,
            sf.cart_discount_codes_update(payload.cart_id, payload.discount_codes, buyer_ip=ip),
            request,
        )
        if isinstance(discount_result, JSONResponse):
            return discount_result
        if discount_result.get("errors"):
            return JSONResponse(
                status_code=422,
                content={"errors": discount_result["errors"], "detail": None},
            )
        result = discount_result

    if isinstance(result, JSONResponse):
        return result
    checkout_url = result.get("checkout_url")
    if not checkout_url:
        return _err(502, "Store did not return a checkout URL")
    return {"checkout_url": checkout_url, "cart": result}


@router.get("/cart/status")
async def overlay_cart_status(
    cart_id: str = Query(...),
    shop: Optional[str] = None,
    request: Request = None,
    sf: StorefrontService = Depends(resolve_storefront),
) -> Any:
    if not sf.is_configured:
        return _err(400, "Store not configured for overlay")
    return await _run(sf, sf.cart_get(cart_id, buyer_ip=_client_ip(request)), request)