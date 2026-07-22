from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class ProductOfferCreate(BaseModel):
    platform_id: str = Field(..., max_length=255)
    product_name: str = Field(..., max_length=500)
    offer_type: str = Field(default="promotion", pattern=r"^(promotion|dead_stock|new_arrival|seasonal)$")
    title: str = Field(..., max_length=500)
    description: Optional[str] = None
    discount_percent: Optional[float] = Field(None, ge=0, le=100)
    discount_amount: Optional[float] = Field(None, ge=0)
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    priority: int = Field(default=0, ge=0)


class ProductOfferUpdate(BaseModel):
    title: Optional[str] = Field(None, max_length=500)
    description: Optional[str] = None
    discount_percent: Optional[float] = Field(None, ge=0, le=100)
    discount_amount: Optional[float] = Field(None, ge=0)
    is_active: Optional[bool] = None
    ends_at: Optional[datetime] = None
    priority: Optional[int] = Field(None, ge=0)


class ProductOfferOut(BaseModel):
    id: str
    tenant_id: str
    platform_id: str
    product_name: str
    offer_type: str
    title: str
    description: Optional[str]
    discount_percent: Optional[float]
    discount_amount: Optional[float]
    starts_at: Optional[datetime]
    ends_at: Optional[datetime]
    is_active: bool
    priority: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
