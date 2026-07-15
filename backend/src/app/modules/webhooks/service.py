"""Webhook service — ingest and dispatch platform webhook events."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .repository import WebhookRepository
from .models import WebhookEvent
from ...core.database import set_tenant_guc

logger = logging.getLogger(__name__)

# ── Field alias resolver ──────────────────────────────────────────────────────
# Maps common field name variations from custom store APIs to canonical names.
# Order matters — first match wins.
_ID_ALIASES    = ("id", "platform_id", "product_id", "_id", "item_id")
_SKU_ALIASES   = ("sku", "variant_id")   # fallback ONLY if no real ID found
_NAME_ALIASES  = ("name", "title", "product_name", "item_name", "label")
_PRICE_ALIASES = ("price", "regular_price", "sale_price", "cost",
                  "selling_price", "unit_price", "amount")
_STOCK_ALIASES = ("in_stock", "available", "stock", "inventory", "is_available")
_IMAGE_ALIASES = ("image_url", "image", "thumbnail", "photo", "picture")
_CAT_ALIASES   = ("category_slug", "category", "category_name", "type")
_DESC_ALIASES  = ("description", "details", "body", "summary", "about")
_URL_ALIASES   = ("permalink", "url", "link", "product_url")


def _resolve_field(product: dict, aliases: tuple, default=None):
    for key in aliases:
        if key in product and product[key] is not None:
            return product[key]
    return default


def _parse_price(raw) -> "float | None":
    """Strip currency symbols / commas, return float. None if not parseable."""
    if raw is None:
        return None
    cleaned = re.sub(r"[₹$€£¥,\s]", "", str(raw)).strip()
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def _parse_stock(raw) -> "bool | None":
    """Normalise stock to True/False. Returns None when the value is ambiguous.

    Handles: booleans, integers (0 = False, >0 = True), and string forms
    ("yes"/"no", "true"/"false", "in stock"/"out of stock", etc.).
    A plain non-empty string that doesn't match any known pattern returns None
    rather than defaulting to True — callers treat None as 'unknown'.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw > 0
    s = str(raw).strip().lower()
    if s in ("true", "yes", "1", "in stock", "in-stock", "available", "instock"):
        return True
    if s in ("false", "no", "0", "out of stock", "out-of-stock", "unavailable",
             "sold out", "soldout", "oos"):
        return False
    return None  # unknown string — treated as stock-unknown by callers


# ── Topic → handler map ───────────────────────────────────────────────────────
# Each handler receives (tenant_id, payload_dict) and returns True on success.
_HANDLERS: dict = {}


def _register(topic: str):
    """Decorator to register a topic handler."""
    def decorator(fn):
        _HANDLERS[topic] = fn
        return fn
    return decorator


# ── Shopify handlers ──────────────────────────────────────────────────────────

def _map_order_status(payload: dict) -> str:
    """Map a Shopify order's financial state to our orders.status.

    Analytics counts only status == "completed" toward revenue/purchases, so a
    paid order must land as "completed". Cancelled/refunded/voided → "cancelled";
    everything else (authorized, pending, unpaid) stays "pending".
    """
    if payload.get("cancelled_at"):
        return "cancelled"
    fin = str(payload.get("financial_status") or "").lower()
    if fin in ("paid", "partially_refunded"):
        return "completed"
    if fin in ("refunded", "voided"):
        return "cancelled"
    return "pending"


def _parse_order_ts(raw) -> datetime:
    """Parse a Shopify ISO timestamp; fall back to now() so a bad value never
    blocks capture. Analytics windows revenue on orders.created_at, so we keep
    the store's real order time when available."""
    if raw:
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass
    return datetime.now(timezone.utc)


async def _upsert_order(tenant_id: str, payload: dict, *, status_override: "str | None" = None) -> None:
    """Idempotently persist a Shopify order into the orders table.

    Keyed on (tenant_id, platform_order_id) so redeliveries and the
    create/paid/updated topics for the same order upsert one row. Opens its own
    session and sets the RLS GUC — orders is RLS-forced, so an unscoped INSERT
    would be rejected by the tenant_isolation WITH CHECK policy.
    """
    platform_order_id = str(payload.get("id") or payload.get("order_id") or "").strip()
    if not platform_order_id:
        logger.warning("Order webhook with no id: tenant=%s", tenant_id)
        return

    total = _parse_price(payload.get("total_price") or payload.get("current_total_price")) or 0.0
    currency = str(payload.get("currency") or payload.get("presentment_currency") or "USD")
    email = (
        payload.get("email")
        or payload.get("contact_email")
        or (payload.get("customer") or {}).get("email")
    )
    status = status_override or _map_order_status(payload)
    created_at = _parse_order_ts(payload.get("created_at"))

    from ...core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        await set_tenant_guc(db, tenant_id)
        await db.execute(text("""
            INSERT INTO orders (
                id, tenant_id, platform_order_id, status, total, currency,
                customer_email, created_at, updated_at
            ) VALUES (
                :id, :tenant_id, :platform_order_id, :status, :total, :currency,
                :customer_email, :created_at, now()
            )
            ON CONFLICT (tenant_id, platform_order_id) DO UPDATE SET
                status         = EXCLUDED.status,
                total          = EXCLUDED.total,
                currency       = EXCLUDED.currency,
                customer_email = EXCLUDED.customer_email,
                updated_at     = now()
        """), {
            "id":                str(uuid.uuid4()),
            "tenant_id":         tenant_id,
            "platform_order_id": platform_order_id,
            "status":            status,
            "total":             total,
            "currency":          currency,
            "customer_email":    email,
            "created_at":        created_at,
        })
        await db.commit()


@_register("orders/create")
@_register("orders/updated")
@_register("orders/paid")
async def _handle_order(tenant_id: str, payload: dict) -> bool:
    """Capture/refresh a Shopify order in the orders table (feeds analytics)."""
    order_id = payload.get("id") or payload.get("order_id", "")
    logger.info("Webhook order event: tenant=%s order_id=%s", tenant_id, order_id)
    try:
        await _upsert_order(tenant_id, payload)
    except Exception as exc:
        # Match the module convention: log + ack so a bad payload can't jam the
        # 60s queue. ERROR level so Sentry surfaces it once activated.
        logger.error(
            "Order capture failed tenant=%s order=%s: %s", tenant_id, order_id, exc, exc_info=True
        )
    return True


@_register("orders/cancelled")
async def _handle_order_cancelled(tenant_id: str, payload: dict) -> bool:
    order_id = payload.get("id", "")
    logger.info("Webhook order cancelled: tenant=%s order_id=%s", tenant_id, order_id)
    try:
        await _upsert_order(tenant_id, payload, status_override="cancelled")
    except Exception as exc:
        logger.error(
            "Order cancel capture failed tenant=%s order=%s: %s", tenant_id, order_id, exc, exc_info=True
        )
    return True


@_register("products/create")
@_register("products/update")
async def _handle_product_update(tenant_id: str, payload: dict) -> bool:
    """Trigger an immediate product re-sync so the search cache stays fresh."""
    product_id = payload.get("id") or payload.get("product_id", "")
    logger.info("Webhook product update: tenant=%s product_id=%s", tenant_id, product_id)
    try:
        from ...workers.tasks.sync_products import sync_products
        sync_products.delay(tenant_id=tenant_id)
        logger.info("Product sync queued for tenant=%s", tenant_id)
    except Exception as exc:
        logger.warning("Could not queue product sync: %s", exc)
    return True


@_register("products/delete")
async def _handle_product_delete(tenant_id: str, payload: dict) -> bool:
    """Remove deleted product from product_cache and re-sync."""
    product_id = payload.get("id") or payload.get("product_id", "")
    logger.info("Webhook product delete: tenant=%s product_id=%s", tenant_id, product_id)
    try:
        from sqlalchemy import text as sqla_text
        from ...core.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            await db.execute(
                sqla_text(
                    "DELETE FROM product_cache WHERE tenant_id = :tid AND platform_id = :pid"
                ),
                {"tid": tenant_id, "pid": str(product_id)},
            )
            await db.commit()
    except Exception as exc:
        logger.warning("Could not delete product_cache row: %s", exc)
    return True


@_register("app/uninstalled")
async def _handle_app_uninstalled(tenant_id: str, payload: dict) -> bool:
    """Mark the tenant inactive when the merchant uninstalls the app."""
    shop = payload.get("domain") or payload.get("myshopify_domain", "")
    logger.warning("App uninstalled: tenant=%s shop=%s", tenant_id, shop)
    # Future: set tenant.is_active = False, cancel subscription
    return True


# ── WooCommerce handlers ──────────────────────────────────────────────────────

@_register("woocommerce_new_order")
async def _handle_woo_new_order(tenant_id: str, payload: dict) -> bool:
    order_id = payload.get("id", "")
    logger.info("WooCommerce new order: tenant=%s order_id=%s", tenant_id, order_id)
    return True


@_register("woocommerce_order_status_changed")
async def _handle_woo_order_status(tenant_id: str, payload: dict) -> bool:
    order_id = payload.get("id", "")
    status = payload.get("status", "")
    logger.info("WooCommerce order status: tenant=%s order_id=%s status=%s", tenant_id, order_id, status)
    return True


@_register("woocommerce_product_updated")
async def _handle_woo_product(tenant_id: str, payload: dict) -> bool:
    product_id = payload.get("id", "")
    logger.info("WooCommerce product updated: tenant=%s product_id=%s", tenant_id, product_id)
    try:
        from ...workers.tasks.sync_products import sync_products
        sync_products.delay(tenant_id=tenant_id)
    except Exception as exc:
        logger.warning("Could not queue product sync: %s", exc)
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# Service
# ═══════════════════════════════════════════════════════════════════════════════

class WebhookService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = WebhookRepository(db)

    async def invalidate_search_cache(self, tenant_id: str) -> None:
        """Purge the L1/L2 retrieval cache for a tenant after product_cache writes.

        Best-effort: a Redis/cache outage must never fail the webhook/ingest. Call
        once per batch (not per product) — invalidate_tenant does a KEYS scan.
        """
        try:
            from ...core.cache import get_redis
            from ...agent.retrieval.cache import invalidate_tenant
            await invalidate_tenant(get_redis(), tenant_id)
        except Exception as exc:
            logger.warning("Search cache invalidation failed tenant=%s: %s", tenant_id, exc)

    async def upsert_product(self, tenant_id: str, product: dict) -> dict:
        """
        Directly upsert a single product into product_cache.
        Used by the custom platform webhook and bulk ingest endpoints.
        The search_vector column is auto-populated by the DB trigger (migration 0005).

        Returns {"ok": True} on success, or {"ok": False, "reason": ..., "keys_seen": [...]}
        so callers can build rejection reports without silent data loss.
        """
        keys_seen = list(product.keys())

        # ── Resolve platform_id (real ID first, SKU only as last resort) ─────
        platform_id = str(_resolve_field(product, _ID_ALIASES, "") or "").strip()
        if not platform_id:
            platform_id = str(_resolve_field(product, _SKU_ALIASES, "") or "").strip()
        if not platform_id:
            logger.warning(
                "Skipping product with no resolvable id: tenant=%s keys=%s",
                tenant_id, keys_seen,
            )
            return {"ok": False, "reason": "no resolvable id field", "keys_seen": keys_seen}

        # ── Resolve name ─────────────────────────────────────────────────────
        name = str(_resolve_field(product, _NAME_ALIASES, "") or "").strip()
        if not name:
            logger.warning(
                "Skipping product with no name: tenant=%s platform_id=%s keys=%s",
                tenant_id, platform_id, keys_seen,
            )
            return {"ok": False, "reason": "no resolvable name field", "keys_seen": keys_seen}

        # ── Resolve and parse price ───────────────────────────────────────────
        raw_price = _resolve_field(product, _PRICE_ALIASES)
        if raw_price is None:
            # Price field completely absent — reject with a clear reason
            logger.warning(
                "Skipping product with no price field: tenant=%s platform_id=%s keys=%s",
                tenant_id, platform_id, keys_seen,
            )
            return {"ok": False, "reason": "price field not found", "keys_seen": keys_seen}
        price = _parse_price(raw_price)
        if price is None:
            logger.warning(
                "Skipping product with unparseable price: tenant=%s platform_id=%s raw=%r",
                tenant_id, platform_id, raw_price,
            )
            return {
                "ok": False,
                "reason": "price not parseable",
                "keys_seen": keys_seen,
                "raw_value": str(raw_price),
            }

        # ── Resolve stock — default None (unknown), NOT True ─────────────────
        # Defaulting to True would bake in hallucination: the LLM would claim
        # "in stock" for products whose actual availability is unknown.
        # _parse_stock() handles strings like "no"/"false" correctly — plain
        # bool() would treat any non-empty string as True (bug).
        in_stock_raw = _resolve_field(product, _STOCK_ALIASES, None)
        in_stock = _parse_stock(in_stock_raw)

        # ── Tags ─────────────────────────────────────────────────────────────
        tags = _resolve_field(product, ("tags",), "")
        if isinstance(tags, list):
            tags = ",".join(str(t) for t in tags)

        # ── Stock quantity ────────────────────────────────────────────────────
        # Use _resolve_field (not `or`) so that stock_quantity=0 is preserved
        # correctly — `or` would skip 0 (falsy) and check the next alias.
        _STOCK_QTY_ALIASES = ("stock_quantity", "qty", "quantity", "stock_count")
        raw_qty = _resolve_field(product, _STOCK_QTY_ALIASES)
        stock_qty: "int | None" = None
        if raw_qty is not None:
            try:
                stock_qty = int(raw_qty)
            except (TypeError, ValueError):
                stock_qty = None

        await self.db.execute(text("""
            INSERT INTO product_cache (
                id, tenant_id, platform_id, name, description, price, currency,
                image_url, in_stock, category_slug, tags, stock_quantity, permalink, cached_at
            ) VALUES (
                :id, :tenant_id, :platform_id, :name, :description, :price, :currency,
                :image_url, :in_stock, :category_slug, :tags, :stock_quantity, :permalink, :cached_at
            )
            ON CONFLICT (tenant_id, platform_id) DO UPDATE SET
                name          = EXCLUDED.name,
                description   = EXCLUDED.description,
                price         = EXCLUDED.price,
                currency      = EXCLUDED.currency,
                image_url     = EXCLUDED.image_url,
                in_stock      = EXCLUDED.in_stock,
                category_slug = EXCLUDED.category_slug,
                tags          = EXCLUDED.tags,
                stock_quantity= EXCLUDED.stock_quantity,
                permalink     = EXCLUDED.permalink,
                cached_at     = EXCLUDED.cached_at
        """), {
            "id":            str(uuid.uuid4()),
            "tenant_id":     tenant_id,
            "platform_id":   platform_id,
            "name":          name,
            "description":   str(_resolve_field(product, _DESC_ALIASES, "") or ""),
            "price":         price,
            "currency":      str(product.get("currency") or "USD"),
            "image_url":     _resolve_field(product, _IMAGE_ALIASES),
            "in_stock":      in_stock,
            "category_slug": _resolve_field(product, _CAT_ALIASES),
            "tags":          tags or None,
            "stock_quantity": stock_qty,
            "permalink":     _resolve_field(product, _URL_ALIASES),
            "cached_at":     datetime.now(timezone.utc),
        })
        await self.db.commit()
        return {"ok": True}

    async def ingest(self, tenant_id: str, topic: str, platform: str, payload: dict):
        """Persist a webhook event, skipping exact redeliveries.

        dedup_key = sha256(topic|payload); the unique (tenant_id, dedup_key)
        constraint makes a redelivered webhook a no-op instead of a duplicate row.
        """
        payload_json = json.dumps(payload, sort_keys=True)
        dedup_key = hashlib.sha256(f"{topic}|{payload_json}".encode("utf-8")).hexdigest()
        event = WebhookEvent(
            tenant_id=tenant_id,
            topic=topic,
            platform=platform,
            payload=payload_json,
            dedup_key=dedup_key,
        )
        try:
            return await self.repo.create(event)
        except IntegrityError:
            await self.db.rollback()
            logger.info("Duplicate webhook ignored: tenant=%s topic=%s", tenant_id, topic)
            existing = await self.db.execute(
                select(WebhookEvent).where(
                    WebhookEvent.tenant_id == tenant_id,
                    WebhookEvent.dedup_key == dedup_key,
                )
            )
            return existing.scalars().first()

    async def process_pending(self) -> int:
        """Fetch pending events, dispatch to topic handlers, mark processed.

        Returns the number of events successfully processed.
        """
        events = await self.repo.list_pending()
        processed = 0

        for event in events:
            # Scope RLS to THIS event's tenant before dispatch — the handler may
            # upsert product_cache / orders (RLS tables) via self.db. The pending
            # scan above is on webhook_events (RLS-excluded), so it ran un-scoped.
            await set_tenant_guc(self.db, event.tenant_id)
            handler = _HANDLERS.get(event.topic)
            try:
                payload = json.loads(event.payload) if event.payload else {}
                if handler:
                    success = await handler(event.tenant_id, payload)
                else:
                    # Unknown topic — log and mark as processed to avoid retrying forever
                    logger.debug(
                        "No handler for webhook topic=%s platform=%s tenant=%s",
                        event.topic, event.platform, event.tenant_id,
                    )
                    success = True

                if success:
                    await self.repo.mark_processed(event.id)
                    processed += 1

            except Exception as exc:
                logger.error(
                    "Webhook dispatch error: event_id=%s topic=%s error=%s",
                    event.id, event.topic, exc,
                    exc_info=True,
                )
                # Leave status as "pending" — Celery will retry on next tick

        return processed
