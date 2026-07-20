from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from sqlalchemy import text

from .config import settings
from .api.v1.router import api_router
from .api.v1.voice import router as voice_router

logger = logging.getLogger(__name__)


def _truthy(val: str | None) -> bool:
    return (val or "").strip().lower() in ("1", "true", "yes", "on")


def _assert_single_process_or_acked() -> None:
    """Guard the single-replica assumption.

    The voice concurrency cap (`_voice_active` in api/v1/voice.py) and the LLM circuit
    breakers (core/circuit_breaker.py) hold state in process memory — correct for ONE
    web process, silently wrong across many (N×voice-cap against a global provider quota;
    unshared breaker state). Until those are Redis-shared (see SCALING.md), refuse to boot
    multi-process unless the operator explicitly acknowledges the risk.
    """
    web_concurrency = int(os.getenv("WEB_CONCURRENCY", "1") or "1")
    declared_replicas = int(os.getenv("SPEAKO_WEB_REPLICAS", "1") or "1")
    if web_concurrency <= 1 and declared_replicas <= 1:
        return
    msg = (
        "Multi-process web detected (WEB_CONCURRENCY=%s, SPEAKO_WEB_REPLICAS=%s), but the "
        "voice concurrency cap and LLM circuit breakers are PER-PROCESS. Make them "
        "Redis-shared before scaling out (see backend/SCALING.md → 'Scaling out')."
    ) % (web_concurrency, declared_replicas)
    if _truthy(os.getenv("SPEAKO_ALLOW_MULTI_PROCESS")):
        logger.warning("%s — proceeding because SPEAKO_ALLOW_MULTI_PROCESS is set.", msg)
        return
    raise RuntimeError(msg + " Set SPEAKO_ALLOW_MULTI_PROCESS=true to override.")


def _enforce_rls_role(role: str, rolsuper: bool, rolbypassrls: bool, *, rls_enabled: bool) -> None:
    """Decide the RLS-role boot outcome (pure → unit-testable).

    A superuser / BYPASSRLS role ignores RLS policies even with FORCE, so RLS (migration
    0013) would look enabled while providing NO cross-tenant protection. Only enforced when
    RLS is actually on the tables — so local dev (superuser role, 0013 not applied) still boots.
    """
    if not (rolsuper or rolbypassrls):
        return  # (f, f) — safe role
    detail = "DB role '%s' has rolsuper=%s rolbypassrls=%s — it BYPASSES Row-Level Security" % (
        role, rolsuper, rolbypassrls,
    )
    if not rls_enabled:
        # RLS not applied yet → the role is currently harmless, but will silently neuter RLS
        # the moment 0013 is enabled. Warn loudly; don't block a pre-RLS environment.
        logger.warning(
            "%s. RLS is not yet enabled on product_cache (migration 0013 not applied) — fix the "
            "app role to NOSUPERUSER NOBYPASSRLS BEFORE enabling RLS, or it will be inert.", detail,
        )
        return
    msg = (
        detail + ", so RLS (migration 0013) is silently INERT and provides no cross-tenant "
        "protection. Use a NOSUPERUSER NOBYPASSRLS app role (see backend/SCALING.md)."
    )
    if _truthy(os.getenv("SPEAKO_ALLOW_RLS_BYPASS")):
        logger.warning("%s — proceeding because SPEAKO_ALLOW_RLS_BYPASS is set.", msg)
        return
    raise RuntimeError(msg + " Set SPEAKO_ALLOW_RLS_BYPASS=true to override.")


async def _assert_rls_role_safe() -> None:
    """On Postgres, refuse to boot if the app's DB role bypasses RLS while RLS is enabled.

    Converts the manual `SELECT rolsuper, rolbypassrls` pre-prod check into an automatic
    boot-time assertion. SQLite (tests) has no such concept → no-op.
    """
    from .core.database import engine, AsyncSessionLocal

    if engine.dialect.name != "postgresql":
        return
    try:
        async with AsyncSessionLocal() as s:
            role_row = (
                await s.execute(
                    text(
                        "SELECT current_user AS role, rolsuper, rolbypassrls "
                        "FROM pg_roles WHERE rolname = current_user"
                    )
                )
            ).one_or_none()
            # Is RLS actually forced on a representative customer table (0013 applied)?
            rls_on = (
                await s.execute(
                    text(
                        "SELECT relrowsecurity AND relforcerowsecurity "
                        "FROM pg_class WHERE relname = 'product_cache'"
                    )
                )
            ).scalar()
    except Exception as exc:
        logger.warning("RLS-role guard could not query the catalog (%s) — proceeding.", exc)
        return
    if role_row is None:
        logger.warning("RLS-role guard: current_user not found in pg_roles — proceeding.")
        return
    _enforce_rls_role(
        str(role_row.role), bool(role_row.rolsuper), bool(role_row.rolbypassrls),
        rls_enabled=bool(rls_on),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .core.database import init_db
    from .core.cache import init_cache
    from .agent.memory.session import SessionService
    from .agent.beta_logger import get_beta_logger

    _assert_single_process_or_acked()
    await init_db()
    await _assert_rls_role_safe()
    await init_cache()

    # ── Redis ────────────────────────────────────────────────────────────────
    redis_client = None
    try:
        redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
        await redis_client.ping()  # type: ignore[misc]
        logger.info("Redis connected")
    except Exception as exc:
        logger.warning("Redis unavailable, using in-memory fallback: %s", exc)
        redis_client = None

    # ── Store client (platform-aware) ─────────────────────────────────────────
    platform = settings.platform.lower()
    logger.info("Platform: %s", platform)

    # `store_client` here is the LEGACY single-store env client. In multi-tenant
    # mode it is only a fallback default handed to the orchestrator — real traffic
    # builds a per-tenant client from the DB (tenants.shopify_access_token etc.).
    # `store_configured` is True only when this env client's own credentials are
    # set (a genuine single-store deployment). When False we skip every boot-time
    # network probe below, because probing an unconfigured env client just emits
    # noisy failures (e.g. a 401 from the Shopify Admin fallback) that have no
    # bearing on real per-tenant requests.
    if platform == "shopify":
        from .integrations.shopify.client import ShopifyClient
        store_client = ShopifyClient(
            store_domain=settings.shopify_store_domain,
            storefront_token=settings.shopify_storefront_token,
            admin_token=settings.shopify_admin_token,
            api_version=settings.shopify_api_version,
            redis_client=redis_client,
        )
        store_configured = bool(settings.shopify_store_domain and settings.shopify_storefront_token)
        if not store_configured:
            logger.info(
                "Legacy env Shopify credentials not set — multi-tenant mode uses "
                "per-tenant clients from the DB; skipping single-store connectivity probe"
            )
        else:
            try:
                test = await store_client.search_products(query="", limit=1, in_stock_only=False)
                logger.info("Shopify OK — %d product(s) found in connectivity test", len(test))
            except Exception as exc:
                logger.error("Shopify connectivity FAILED: %s", exc)
        _raw_woo = None
    elif platform == "custom_api":
        from .integrations.custom_api.client import CustomApiClient
        store_client = CustomApiClient(
            base_url=settings.custom_api_base_url,
            api_key=settings.custom_api_key,
        )
        store_configured = bool(settings.custom_api_base_url)
        if not store_configured:
            logger.info(
                "CUSTOM_API_BASE_URL not set — per-tenant mode; skipping single-store connectivity probe"
            )
        else:
            try:
                test = await store_client.search_products(query="", limit=1, in_stock_only=False)
                logger.info("Custom API OK — %d product(s) found in connectivity test", len(test))
            except Exception as exc:
                logger.error("Custom API connectivity FAILED: %s", exc)
        _raw_woo = None
    else:
        from .integrations.woocommerce.client import WooCommerceClient
        from .integrations.woocommerce.cache import CachedWooCommerceClient
        _raw_woo = WooCommerceClient(
            store_url=settings.woocommerce_store_url,
            consumer_key=settings.woocommerce_consumer_key,
            consumer_secret=settings.woocommerce_consumer_secret,
            redis_client=redis_client,
        )
        store_client = CachedWooCommerceClient(wc_client=_raw_woo, redis_client=redis_client)
        store_configured = bool(settings.woocommerce_store_url)
        if not store_configured:
            logger.info(
                "WOOCOMMERCE_STORE_URL not set — per-tenant mode; skipping single-store connectivity probe"
            )
        else:
            try:
                test = await store_client.search_products(query="", limit=1, in_stock_only=False)
                logger.info("WooCommerce OK — %d product(s) found in connectivity test", len(test))
            except Exception as exc:
                logger.error("WooCommerce connectivity FAILED: %s", exc)

    # Pre-warm Redis cache — only for a genuinely-configured single-store env client.
    # In multi-tenant mode the env client is an unused fallback, so pre-warming it
    # just fires the same doomed probes (and the 401) we skipped above.
    if store_configured:
        try:
            await store_client.pre_warm()
        except Exception as exc:
            logger.debug("Store pre-warm skipped: %s", exc)

    # Kick off an initial product sync so product_cache is populated on first boot.
    # Runs asynchronously via Celery — does not block startup.
    try:
        from .workers.tasks.sync_products import sync_products
        sync_products.delay()
        logger.info("Initial product sync queued")
    except Exception as exc:
        logger.warning("Could not queue initial product sync (Celery unavailable?): %s", exc)

    # ── Session service ───────────────────────────────────────────────────────
    session_service = SessionService(redis_client=redis_client)

    # ── Beta logger ───────────────────────────────────────────────────────────
    beta_logger = get_beta_logger()
    await beta_logger.start()

    # ── Object Storage (Phase 11) ─────────────────────────────────────────────
    storage_client = None
    try:
        from .agent.voice.object_storage import ObjectStorageClient
        storage_client = ObjectStorageClient.from_settings(settings)
        if storage_client.enabled:
            logger.info("Object storage enabled: provider=%s bucket=%s",
                        settings.object_storage_provider, settings.object_storage_bucket)
    except Exception as exc:
        logger.warning("Object storage unavailable (TTS will use Redis-only cache): %s", exc)

    # ── Audio logger (Phase 11) ───────────────────────────────────────────────
    audio_logger = None
    try:
        from .agent.voice.audio_logger import AudioLogger, get_noop_audio_logger
        if storage_client is not None and storage_client.enabled and settings.audio_logging_enabled:
            audio_logger = AudioLogger(storage_client, enabled=True)
            logger.info("Audio logging enabled")
        else:
            audio_logger = get_noop_audio_logger()
    except Exception as exc:
        logger.warning("Audio logger init failed: %s", exc)

    # ── TTS (optional — only needed for /greet audio) ─────────────────────────
    tts_service = None
    try:
        from .agent.voice.synthesis import TTSServiceV2
        tts_service = TTSServiceV2(redis_client=redis_client, storage_client=storage_client)
        logger.info("TTS service initialised (L2 storage=%s)", storage_client.enabled if storage_client else False)
    except Exception as exc:
        logger.warning("TTS service unavailable (greet will use browser TTS): %s", exc)

    # ── Agent orchestrator (shared across HTTP + WS paths) ────────────────────
    from .agent.orchestrator import AgentOrchestrator
    from .core.database import AsyncSessionLocal
    orchestrator = AgentOrchestrator(
        store_client=store_client,
        session_service=session_service,
        tts_service=tts_service,
        redis=redis_client,
        db_session_factory=AsyncSessionLocal,
    )

    app.state.redis = redis_client
    app.state.store_client = store_client
    app.state.session_service = session_service
    app.state.tts_service = tts_service
    app.state.storage_client = storage_client
    app.state.audio_logger = audio_logger
    app.state.orchestrator = orchestrator

    logger.info("Backend ready")
    try:
        yield
    finally:
        try:
            await store_client.close()
        except Exception:
            pass
        if _raw_woo is not None:
            try:
                await _raw_woo.close()
            except Exception:
                pass
        try:
            await beta_logger.close()
        except Exception:
            pass
        if redis_client is not None:
            try:
                await redis_client.aclose()
            except Exception:
                pass
        logger.info("Backend shutdown complete")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        lifespan=lifespan,
    )

    # Error tracking. No-op when SENTRY_DSN is unset, so behavior is unchanged
    # until a DSN is provided. Init here (at import time) so it also wraps the
    # lifespan startup — including the RLS boot guard's RuntimeError.
    if settings.sentry_dsn:
        import sentry_sdk
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.sentry_environment or settings.environment,
            traces_sample_rate=settings.sentry_traces_sample_rate,
        )

    # Translate domain AppError subclasses (UnauthorizedError→401, ForbiddenError→403,
    # NotFoundError→404, …) into proper HTTP responses. Without this, every raised
    # AppError bubbles up as a 500 — e.g. a wrong-password login would 500 instead of 401.
    from .core.exceptions import AppError
    from .core.middleware import app_error_handler
    app.add_exception_handler(AppError, app_error_handler)

    @app.middleware("http")
    async def add_cors_to_static(request: Request, call_next) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "*"
            response.headers["ngrok-skip-browser-warning"] = "true"
        return response

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next) -> Response:
        # Safe, widget-compatible headers. CORS stays permissive by design (the
        # widget loads cross-origin from arbitrary merchant domains, credentials
        # off). X-Frame-Options omitted to avoid breaking Shopify-admin embedding.
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if settings.environment.lower() in ("production", "prod"):
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Widget endpoints under /api/v1 (greet, chat, module routers)
    app.include_router(api_router, prefix="/api/v1")
    # Voice WebSocket at root-level /wooagent/stream (no /api/v1 prefix)
    # Widget JS connects to: ws://host/wooagent/stream?session_id=...&token=...
    app.include_router(voice_router)
    # Serve widget JS + CSS at /static/wooagent-widget.js
    # Shopify widget-loader.js loads this URL after setting window.wooagent_config
    app.mount("/static", StaticFiles(directory="static"), name="static")
    return app


app = create_app()
