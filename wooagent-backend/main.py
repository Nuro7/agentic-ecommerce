from __future__ import annotations

# load_dotenv() MUST run before any application import because llm_clients.py
# reads API keys at module-level (import time). Calling it after the imports
# means all LLM clients are None and ANY_LLM_AVAILABLE stays False.
from dotenv import load_dotenv
load_dotenv()

import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import List

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from routers import chat, greet, health, live, transcribe
from services.beta_logger import get_beta_logger
from services.rate_limit import limiter
from services.session import SessionService
from services.session_facts import get_session_facts_service
from services.wc_cache import CachedWooCommerceClient
from services.woocommerce import WooCommerceClient

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass
class Settings:
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379")
    woocommerce_store_url: str = os.getenv("WOOCOMMERCE_STORE_URL", "")
    woocommerce_consumer_key: str = os.getenv("WOOCOMMERCE_CONSUMER_KEY", "")
    woocommerce_consumer_secret: str = os.getenv("WOOCOMMERCE_CONSUMER_SECRET", "")
    allowed_origins: str = os.getenv("ALLOWED_ORIGINS", "*")
    store_name: str = os.getenv("STORE_NAME", "My Store")
    store_currency: str = os.getenv("STORE_CURRENCY", "$")
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    shared_secret: str = os.getenv("SHARED_SECRET", "")

    def validate(self) -> None:
        """Emit startup warnings for missing or insecure configuration."""
        if not self.woocommerce_store_url:
            logger.warning("WOOCOMMERCE_STORE_URL is not set — product/cart APIs will fail")
        if not self.shared_secret:
            logger.warning("SHARED_SECRET is not set — HMAC request verification is disabled")
        elif self.shared_secret in {"change-me", "secret", "password", "nif@123", ""}:
            logger.warning(
                "SHARED_SECRET looks like a default/weak value. "
                "Generate a strong secret with: python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        if not self.gemini_api_key:
            logger.warning("GEMINI_API_KEY is not set — Gemini 3.1 Flash Live WebSocket will fail")

    @property
    def cors_origins(self) -> List[str]:
        origins = [item.strip() for item in self.allowed_origins.split(",") if item.strip()]
        return origins or ["*"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    settings.validate()
    app.state.settings = settings

    redis_client = None
    try:
        redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
        await redis_client.ping()
        logger.info("Redis connected")
    except Exception as exc:
        logger.warning("Redis unavailable, using in-memory fallback: %s", exc)
        redis_client = None

    # ── Core services ─────────────────────────────────────────────────────────
    # NOTE: All text/audio interaction is handled by Gemini 3.1 Flash Live Preview
    # via the WebSocket at /wooagent/stream.  OpenAI, Groq, STT, and TTS services
    # are NOT initialised — only WooCommerce client + session store are needed.
    _raw_woo = WooCommerceClient(
        store_url=settings.woocommerce_store_url,
        consumer_key=settings.woocommerce_consumer_key,
        consumer_secret=settings.woocommerce_consumer_secret,
        redis_client=redis_client,
    )
    # Wrap with Redis cache proxy (TTL-aware, write-through bypass)
    woo_client = CachedWooCommerceClient(wc_client=_raw_woo, redis_client=redis_client)

    session_service = SessionService(redis_client=redis_client)

    # Initialise session facts store and beta logger
    get_session_facts_service(redis_client=redis_client)
    beta_logger = get_beta_logger()
    await beta_logger.start()

    app.state.redis = redis_client
    app.state.woo_client = woo_client
    app.state.session_service = session_service

    # ── WooCommerce connectivity check (runs at startup) ──────────────────────
    # Shows immediately whether the store is reachable and which API path works.
    try:
        test_products = await woo_client.search_products(query="", limit=1, in_stock_only=False)
        if test_products:
            logger.info(
                "WooCommerce OK — store reachable, found %d product(s) in connectivity test",
                len(test_products),
            )
        else:
            logger.warning(
                "WooCommerce reachable but returned 0 products — "
                "check that WooCommerce has published products at %s",
                settings.woocommerce_store_url,
            )
    except Exception as wc_err:
        logger.error(
            "WooCommerce connectivity FAILED: %s — "
            "store URL: %s  |  Plugin installed? WC active? Docker extra_hosts correct?",
            wc_err,
            settings.woocommerce_store_url,
        )

    # Pre-warm WC cache in background (categories + sample products)
    try:
        await woo_client.pre_warm()
        logger.info("WC cache pre-warm initiated")
    except Exception as pw_err:
        logger.debug("WC pre-warm skipped: %s", pw_err)

    logger.info("WooAgent backend is ready")
    try:
        yield
    finally:
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
        logger.info("WooAgent backend shutdown complete")


app = FastAPI(
    title="WooAgent Backend",
    version="1.0.0",
    description="Voice-first WooCommerce shopping agent backend",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

_cors_origins = Settings().cors_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    # allow_credentials requires explicit origins (not "*") per CORS spec.
    # The widget never sends cookies or Authorization headers, so False is correct.
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# Primary routes
app.include_router(chat.router)
app.include_router(transcribe.router)
app.include_router(health.router)
app.include_router(greet.router)
app.include_router(live.router) # New WebSockets Route

# Compatibility routes for earlier deployments that used /api/v1 prefix.
app.include_router(chat.router, prefix="/api/v1")
app.include_router(transcribe.router, prefix="/api/v1")
app.include_router(health.router, prefix="/api/v1")
app.include_router(greet.router, prefix="/api/v1")
