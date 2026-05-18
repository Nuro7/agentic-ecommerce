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

    # Pre-warm cache (background)
    try:
        await store_client.pre_warm()
    except Exception as exc:
        logger.debug("Store pre-warm skipped: %s", exc)

    # ── Session service ───────────────────────────────────────────────────────
    session_service = SessionService(redis_client=redis_client)

    # ── Beta logger ───────────────────────────────────────────────────────────
    beta_logger = get_beta_logger()
    await beta_logger.start()

    # ── TTS (optional — only needed for /greet audio) ─────────────────────────
    tts_service = None
    try:
        from .agent.voice.synthesis import TTSServiceV2
        tts_service = TTSServiceV2(redis_client=redis_client)
        logger.info("TTS service initialised")
    except Exception as exc:
        logger.warning("TTS service unavailable (greet will use browser TTS): %s", exc)

    app.state.redis = redis_client
    app.state.store_client = store_client
    app.state.session_service = session_service
    app.state.tts_service = tts_service

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
