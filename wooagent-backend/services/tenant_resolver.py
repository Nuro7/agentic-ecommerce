"""
services/tenant_resolver.py

Resolves a tenant ID to store credentials from the SaaS database,
then creates the correct platform store client (ShopifyClient or
WooCommerceClient) for that tenant.

This is what makes wooagent-backend multi-tenant: instead of reading
credentials from .env at startup, each request carries X-Tenant-ID,
and this module dynamically builds the store client from the DB.

Falls back to the singleton app.state.woo_client when no tenant ID
is provided — preserving backward compatibility with single-tenant mode.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ── DB connection (shared with app/ backend) ──────────────────────────────────
# Uses the same PostgreSQL DATABASE_URL that app/ uses.
# Only imported/used when PLATFORM=saas or X-Tenant-ID header is present.

_db_pool = None  # asyncpg connection pool, lazily initialised


async def _get_pool():
    global _db_pool
    if _db_pool is not None:
        return _db_pool
    try:
        import asyncpg
        db_url = os.getenv("DATABASE_URL", "")
        if not db_url:
            return None
        # asyncpg uses postgresql:// not postgresql+asyncpg://
        pg_url = db_url.replace("postgresql+asyncpg://", "postgresql://").replace("postgresql+psycopg2://", "postgresql://")
        _db_pool = await asyncpg.create_pool(pg_url, min_size=1, max_size=5, command_timeout=5)
        logger.info("Tenant resolver DB pool ready")
    except Exception as exc:
        logger.warning("Tenant resolver DB pool failed (single-tenant mode only): %s", exc)
        _db_pool = None
    return _db_pool


async def close_pool() -> None:
    global _db_pool
    if _db_pool:
        await _db_pool.close()
        _db_pool = None


# ── Encryption (mirrors app/infrastructure/security/encryption.py) ────────────

def _decrypt_credentials(ciphertext: bytes) -> Dict[str, Any]:
    """Decrypt AES-256-GCM credentials blob from the store_connections table."""
    import base64
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key_b64 = os.getenv("ENCRYPTION_KEY", "")
    if key_b64:
        key = base64.b64decode(key_b64)
    else:
        # Dev fallback matching app/infrastructure/security/encryption.py
        secret = os.getenv("JWT_SECRET_KEY", "dev-only-change-me").encode()
        key = (secret * 4)[:32]

    aesgcm = AESGCM(key)
    nonce, ct = ciphertext[:12], ciphertext[12:]
    plaintext = aesgcm.decrypt(nonce, ct, None).decode("utf-8")
    return json.loads(plaintext)


# ── Store client factory ───────────────────────────────────────────────────────

async def get_store_client_for_tenant(
    tenant_id: str,
    redis_client=None,
    fallback_client=None,
):
    """
    Look up the active store connection for tenant_id from the DB,
    decrypt its credentials, and return the appropriate store client.

    Returns fallback_client if:
    - DB is unavailable
    - No active store found for the tenant
    - Any error occurs (fail-open so existing WC stores keep working)
    """
    pool = await _get_pool()
    if pool is None:
        logger.debug("No DB pool — using fallback client for tenant %s", tenant_id)
        return fallback_client

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT sc.id, sc.platform, sc.store_url, sc.credentials_enc,
                       sc.api_version, sc.display_name
                FROM   store_connections sc
                JOIN   tenants t ON t.id = sc.tenant_id
                WHERE  sc.tenant_id = $1::uuid
                  AND  sc.status = 'active'
                  AND  t.status NOT IN ('suspended', 'cancelled')
                ORDER  BY sc.created_at ASC
                LIMIT  1
                """,
                tenant_id,
            )

        if not row:
            logger.warning("No active store for tenant %s — using fallback", tenant_id)
            return fallback_client

        platform = row["platform"]
        credentials_enc: Optional[bytes] = row["credentials_enc"]
        store_url: str = row["store_url"]
        api_version: Optional[str] = row["api_version"]

        if not credentials_enc:
            logger.warning("Store %s has no credentials — using fallback", row["id"])
            return fallback_client

        creds = _decrypt_credentials(bytes(credentials_enc))

        if platform == "shopify":
            from services.shopify import ShopifyClient
            store_domain = store_url.replace("https://", "").replace("http://", "").rstrip("/")
            return ShopifyClient(
                store_domain=store_domain,
                storefront_token=creds.get("storefront_token", ""),
                admin_token=creds.get("admin_token", ""),
                api_version=api_version or creds.get("api_version", "2025-01"),
                redis_client=redis_client,
            )

        elif platform == "wordpress":
            from services.woocommerce import WooCommerceClient
            from services.wc_cache import CachedWooCommerceClient
            wc = WooCommerceClient(
                store_url=store_url,
                consumer_key=creds.get("consumer_key", ""),
                consumer_secret=creds.get("consumer_secret", ""),
                redis_client=redis_client,
            )
            return CachedWooCommerceClient(wc_client=wc, redis_client=redis_client)

        else:
            logger.warning("Unknown platform '%s' for tenant %s", platform, tenant_id)
            return fallback_client

    except Exception as exc:
        logger.error("Tenant resolver failed for %s: %s", tenant_id, exc)
        return fallback_client
