from pydantic import BaseModel, EmailStr, field_validator
from datetime import datetime
from typing import Optional


class TenantCreate(BaseModel):
    name: str
    email: EmailStr
    plan: str = "free"
    platform: str = "shopify"

    # Shopify credentials (required when platform=shopify)
    shopify_domain: Optional[str] = None
    shopify_access_token: Optional[str] = None
    shopify_storefront_token: Optional[str] = None

    # WooCommerce credentials (required when platform=woocommerce)
    woocommerce_store_url: Optional[str] = None
    woocommerce_consumer_key: Optional[str] = None
    woocommerce_consumer_secret: Optional[str] = None

    # Custom API credentials (required when platform=custom_api)
    custom_api_base_url: Optional[str] = None
    custom_api_key: Optional[str] = None

    # Per-tenant store config (optional — env-var fallback applies when unset)
    currency_symbol: Optional[str] = None
    shipping_policy: Optional[str] = None
    returns_policy: Optional[str] = None
    payment_methods: Optional[str] = None
    about_text: Optional[str] = None

    @field_validator("platform")
    @classmethod
    def validate_platform(cls, v: str) -> str:
        allowed = {"shopify", "woocommerce", "custom_api"}
        v = v.lower().strip()
        if v not in allowed:
            raise ValueError(f"platform must be one of: {', '.join(sorted(allowed))}")
        return v


class TenantUpdate(BaseModel):
    name: Optional[str] = None
    plan: Optional[str] = None
    is_active: Optional[bool] = None
    platform: Optional[str] = None

    # Shopify
    shopify_domain: Optional[str] = None
    shopify_access_token: Optional[str] = None
    shopify_storefront_token: Optional[str] = None

    # WooCommerce
    woocommerce_store_url: Optional[str] = None
    woocommerce_consumer_key: Optional[str] = None
    woocommerce_consumer_secret: Optional[str] = None

    # Custom API
    custom_api_base_url: Optional[str] = None
    custom_api_key: Optional[str] = None

    # Per-tenant store config
    currency_symbol: Optional[str] = None
    shipping_policy: Optional[str] = None
    returns_policy: Optional[str] = None
    payment_methods: Optional[str] = None
    about_text: Optional[str] = None

    @field_validator("platform")
    @classmethod
    def validate_platform(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        allowed = {"shopify", "woocommerce", "custom_api"}
        v = v.lower().strip()
        if v not in allowed:
            raise ValueError(f"platform must be one of: {', '.join(sorted(allowed))}")
        return v


class TenantOut(BaseModel):
    id: str
    name: str
    email: str
    plan: str
    platform: str
    is_active: bool
    created_at: datetime

    # Expose store URLs (not secrets) so dashboard can display them
    shopify_domain: Optional[str] = None
    woocommerce_store_url: Optional[str] = None
    custom_api_base_url: Optional[str] = None

    model_config = {"from_attributes": True}
