"""
Bulk product ingest — custom platform initial catalog sync.

POST /api/v1/ingest/products
  Auth:    Authorization: Bearer {custom_api_key}
  Body:    JSON array of product objects (CanonicalProduct shape)
  Returns: { "ingested": N }

This endpoint is the one-shot path for pushing an existing product catalog
into Speako's product_cache so Aria can search it via hybrid BM25 + vector.
After the initial push, use POST /webhooks/custom/{tenant_id} for incremental updates.

Minimal product shape:
  {
    "id":          "101",          ← required
    "name":        "Red Shoes",    ← required
    "price":       49.99,
    "in_stock":    true,
    "description": "...",
    "image_url":   "https://...",
    "category_slug": "shoes",
    "tags":        ["sale", "new"],
    "permalink":   "https://mystore.com/products/red-shoes"
  }
"""
from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.database import get_db
from ...modules.tenants.repository import TenantRepository
from ...modules.webhooks.service import WebhookService

router = APIRouter(prefix="/ingest", tags=["ingest"])


@router.post("/products", status_code=status.HTTP_200_OK)
async def bulk_ingest_products(
    request: Request,
    authorization: str = Header(..., description="Bearer {custom_api_key}"),
    db: AsyncSession = Depends(get_db),
):
    """
    Push product catalog into Speako's search index.

    Used for the initial one-time catalog sync and batch updates.
    Auth is the tenant's custom_api_key set during onboarding.
    """
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header must be: Bearer {custom_api_key}",
        )
    api_key = authorization[7:].strip()

    tenant = await TenantRepository(db).get_by_custom_api_key(api_key)
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key — check your custom_api_key from onboarding",
        )

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body must be valid JSON (a product object or a list of them).",
        )
    products = body if isinstance(body, list) else [body]

    if not products:
        return {"ingested": 0, "skipped": 0, "tenant_id": str(tenant.id), "rejections": []}

    svc = WebhookService(db)
    ingested = 0
    skipped = 0
    # key: rejection reason → {count, keys_seen (first occurrence), raw_value}
    rejection_tracker: dict = defaultdict(
        lambda: {"count": 0, "keys_seen": [], "raw_value": None}
    )

    for item in products:
        if not isinstance(item, dict):
            skipped += 1
            continue
        outcome = await svc.upsert_product(tenant.id, item)
        if outcome.get("ok"):
            ingested += 1
        else:
            skipped += 1
            reason = outcome.get("reason", "unknown")
            entry = rejection_tracker[reason]
            entry["count"] += 1
            if not entry["keys_seen"]:
                entry["keys_seen"] = outcome.get("keys_seen", [])[:10]
            if entry["raw_value"] is None and "raw_value" in outcome:
                entry["raw_value"] = outcome["raw_value"]

    # Purge stale search cache once, after the whole batch is written.
    if ingested:
        await svc.invalidate_search_cache(tenant.id)

    rejections = []
    for reason, data in rejection_tracker.items():
        hint_parts = []
        if data["keys_seen"]:
            hint_parts.append(f"your product objects have keys: {data['keys_seen']}")
        if data["raw_value"] is not None:
            hint_parts.append(f"raw value was {data['raw_value']!r}")
        rejections.append({
            "reason": reason,
            "count": data["count"],
            "hint": " — ".join(hint_parts) if hint_parts else "",
        })

    return {
        "ingested":  ingested,
        "skipped":   skipped,
        "tenant_id": str(tenant.id),
        "rejections": rejections,
    }
