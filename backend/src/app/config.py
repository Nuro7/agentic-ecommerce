from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "Agentic Commerce"
    version: str = "0.1.0"
    environment: str = "dev"
    debug: bool = False
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://agentic:agentic@localhost:5432/agentic_commerce"
    redis_url: str = "redis://localhost:6379/0"

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

    openai_api_key: str = ""
    groq_api_key: str = ""
    gemini_api_key: str = ""

    google_tts_api_key: str = ""
    elevenlabs_api_key: str = ""
    tts_provider: str = "google"

    store_name: str = "My Store"
    store_currency: str = "$"

    encryption_key: str = ""
    shared_secret: str = "change-me"
    ngrok_authtoken: str = ""

    @property
    def is_shopify(self) -> bool:
        return self.platform.lower() == "shopify"

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
