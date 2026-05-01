"""Application configuration using Pydantic Settings.

Reads from environment variables and .env file. Validates types at startup.
Required values fail loudly with a clear error if missing.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, RedisDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment and .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    app_name: str = "Agentic Commerce"
    app_version: str = "0.1.0"
    environment: Literal["dev", "staging", "prod"] = "dev"
    debug: bool = False
    log_level: str = "INFO"

    # Database (Supabase)
    # Pooled URL (port 6543, transaction mode) for app runtime.
    # Direct URL (port 5432) for Alembic migrations.
    # Stored as plain str rather than PostgresDsn because Supabase URLs
    # contain dots/dashes in the username that confuse strict validators.
    database_url: str
    database_direct_url: str
    database_pool_size: int = 5
    database_max_overflow: int = 2

    # Redis
    redis_url: RedisDsn

    # Security (placeholders — fully populated in Module 3)
    jwt_secret_key: SecretStr = Field(default=SecretStr("dev-only-change-me"))
    jwt_algorithm: str = "HS256"
    jwt_access_ttl_minutes: int = 15
    jwt_refresh_ttl_days: int = 7

    # CORS
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # External services (placeholders for later modules)
    gemini_api_key: SecretStr | None = None
    razorpay_key_id: str | None = None
    razorpay_key_secret: SecretStr | None = None

    @property
    def is_dev(self) -> bool:
        return self.environment == "dev"


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance — call this throughout the app."""
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
