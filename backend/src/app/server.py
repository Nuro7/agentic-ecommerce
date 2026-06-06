from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from .config import settings
from .api.v1.router import api_router
from .api.v1.voice import router as voice_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .core.database import init_db
    from .core.cache import init_cache
    from .agent.memory.session import SessionService
    from .agent.beta_logger import get_beta_logger

    await init_db()
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

    if platform == "shopify":
        from .integrations.shopify.client import ShopifyClient
        store_client = ShopifyClient(
            store_domain=settings.shopify_store_domain,
            storefront_token=settings.shopify_storefront_token,
            admin_token=settings.shopify_admin_token,
            api_version=settings.shopify_api_version,
            redis_client=redis_client,
        )
        if not settings.shopify_store_domain or not settings.shopify_storefront_token:
            logger.warning("SHOPIFY_STORE_DOMAIN or SHOPIFY_STOREFRONT_TOKEN not set — product/cart APIs will fail")
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
        if not settings.custom_api_base_url:
            logger.warning("CUSTOM_API_BASE_URL not set — product/cart APIs will fail")
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
        if not settings.woocommerce_store_url:
            logger.warning("WOOCOMMERCE_STORE_URL not set — product/cart APIs will fail")
        else:
            try:
                test = await store_client.search_products(query="", limit=1, in_stock_only=False)
                logger.info("WooCommerce OK — %d product(s) found in connectivity test", len(test))
            except Exception as exc:
                logger.error("WooCommerce connectivity FAILED: %s", exc)

    # Pre-warm Redis cache (background)
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

    @app.middleware("http")
    async def add_cors_to_static(request: Request, call_next) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "*"
            response.headers["ngrok-skip-browser-warning"] = "true"
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
