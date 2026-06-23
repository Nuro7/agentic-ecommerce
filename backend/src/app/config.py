from functools import lru_cache
from pydantic import model_validator
from pydantic_settings import BaseSettings

# Secret values that must never reach production (placeholders shipped in .env.example).
_WEAK_SECRETS = {
    "", "change-me", "change-me-in-production",
    "change-me-in-production-this-is-just-for-dev", "nif@123",
}


class Settings(BaseSettings):
    app_name: str = "Agentic Commerce"
    version: str = "0.1.0"
    # Fail-safe default: production. A deployment that forgets to set ENVIRONMENT
    # must NOT silently fall back to dev (dev disables the SSRF guard in onboarding
    # and skips the production-secret checks). Local/dev sets ENVIRONMENT=dev in .env.
    environment: str = "production"
    debug: bool = False
    log_level: str = "INFO"

    # Multi-tenant safety: when true, a request/WS whose tenant can't be resolved
    # (no ?shop= / X-Tenant-ID / tenant_id) is REJECTED instead of falling back to
    # the global app.state.store_client — which would collapse two tenants onto one
    # store and merge their catalogs/sessions. Left as None it defaults to "on in
    # production" (see require_tenant). Set ENFORCE_TENANT_RESOLUTION=false to deploy
    # the backend dark during a widget-first rollout, then flip it on once every
    # widget sends a tenant id on all paths.
    enforce_tenant_resolution: bool | None = None

    database_url: str = "postgresql+asyncpg://agentic:agentic@localhost:5432/agentic_commerce"
    # Pool sizing is now read from config (was hardcoded). Size against the DB's
    # max_connections budget across ALL web replicas + Celery workers.
    database_pool_size: int = 20
    database_max_overflow: int = 40
    redis_url: str = "redis://localhost:6379/0"
    # Optional dedicated Redis for the Celery broker/result backend. Defaults to
    # redis_url; set separately in prod so a cache stampede can't stall the queue.
    celery_broker_url: str = ""

    jwt_secret_key: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:8080"]

    platform: str = "woocommerce"

    woocommerce_store_url: str = ""
    woocommerce_consumer_key: str = ""
    woocommerce_consumer_secret: str = ""

    shopify_store_domain: str = ""
    shopify_storefront_token: str = ""
    shopify_admin_token: str = ""
    shopify_api_version: str = "2024-01"
    shopify_api_key: str = ""
    shopify_api_secret: str = ""

    custom_api_base_url: str = ""
    custom_api_key: str = ""

    openai_api_key: str = ""
    groq_api_key: str = ""
    gemini_api_key: str = ""
    grok_api_key: str = ""       # xAI Grok STT (Pipeline B streaming STT)
    stt_provider: str = "grok"   # grok | groq | deepgram

    google_tts_api_key: str = ""
    elevenlabs_api_key: str = ""
    tts_provider: str = "google"

    store_currency: str = "$"

    encryption_key: str = ""
    shared_secret: str = "change-me"
    ngrok_authtoken: str = ""

    # ── Object Storage (Phase 11) — all optional, storage disabled when unset ──
    # OBJECT_STORAGE_PROVIDER: s3 | r2 | gcs | disabled
    object_storage_provider: str = "disabled"
    object_storage_bucket: str = ""
    object_storage_region: str = "us-east-1"
    object_storage_endpoint: str = ""   # required for R2 / GCS
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    # Set to "true" to persist raw voice I/O audio to object storage
    audio_logging_enabled: bool = False

    @property
    def is_shopify(self) -> bool:
        return self.platform.lower() == "shopify"

    @property
    def require_tenant(self) -> bool:
        """Whether to reject requests with no resolvable tenant.

        Explicit ENFORCE_TENANT_RESOLUTION wins; otherwise enforce in production.
        """
        if self.enforce_tenant_resolution is not None:
            return self.enforce_tenant_resolution
        return self.environment.lower() in ("production", "prod")

    @model_validator(mode="after")
    def _enforce_production_secrets(self) -> "Settings":
        """Fail fast if production is started with default/placeholder secrets."""
        if self.environment.lower() in ("production", "prod"):
            weak = []
            if self.jwt_secret_key in _WEAK_SECRETS:
                weak.append("JWT_SECRET_KEY")
            if self.shared_secret in _WEAK_SECRETS:
                weak.append("SHARED_SECRET")
            if weak:
                raise ValueError(
                    "Refusing to start in production with default/placeholder secrets: "
                    + ", ".join(weak)
                    + ". Set strong random values."
                )
            if self.debug:
                raise ValueError("DEBUG must be false in production.")
        return self

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
