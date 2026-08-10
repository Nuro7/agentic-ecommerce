from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field, model_validator


class ComboItem(BaseModel):
    platform_id: str = Field(..., max_length=255)
    quantity: int = Field(default=1, ge=1, le=999)
    name: str = Field(default="", max_length=500)


class BulkTier(BaseModel):
    min_qty: int = Field(..., ge=1)
    discount_percent: Optional[float] = Field(None, ge=0, le=100)
    discount_amount: Optional[float] = Field(None, ge=0)


class ProductOfferCreate(BaseModel):
    platform_id: Optional[str] = Field(None, max_length=255)
    product_name: Optional[str] = Field(None, max_length=500)
    offer_type: str = Field(default="promotion", pattern=r"^(promotion|dead_stock|new_arrival|seasonal)$")
    offer_kind: str = Field(default="discount", pattern=r"^(discount|dead_stock|combo|bulk)$")
    title: str = Field(..., max_length=500)
    description: Optional[str] = None
    discount_percent: Optional[float] = Field(None, ge=0, le=100)
    discount_amount: Optional[float] = Field(None, ge=0)
    combo_items: Optional[List[ComboItem]] = None
    combo_price: Optional[float] = Field(None, ge=0)
    bulk_tiers: Optional[List[BulkTier]] = None
    max_redemptions: Optional[int] = Field(None, ge=1)
    inventory_threshold: Optional[int] = Field(None, ge=0)
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    priority: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_kind_shape(self):
        if self.offer_kind in ("combo", "bulk", "dead_stock") and self.offer_kind != "discount":
            if self.offer_kind == "combo":
                if not self.combo_items or not self.combo_price:
                    raise ValueError("combo offers require combo_items and combo_price")
            elif self.offer_kind == "bulk":
                if not self.bulk_tiers:
                    raise ValueError("bulk offers require bulk_tiers")
                sorted_tiers = sorted(self.bulk_tiers, key=lambda t: t.min_qty)
                if [t.min_qty for t in sorted_tiers] != [t.min_qty for t in self.bulk_tiers]:
                    raise ValueError("bulk_tiers must be ordered ascending by min_qty")
                for tier in self.bulk_tiers:
                    if tier.discount_percent is None and tier.discount_amount is None:
                        raise ValueError("each bulk tier needs discount_percent or discount_amount")
        return self


class ProductOfferUpdate(BaseModel):
    title: Optional[str] = Field(None, max_length=500)
    description: Optional[str] = None
    discount_percent: Optional[float] = Field(None, ge=0, le=100)
    discount_amount: Optional[float] = Field(None, ge=0)
    combo_items: Optional[List[ComboItem]] = None
    combo_price: Optional[float] = Field(None, ge=0)
    bulk_tiers: Optional[List[BulkTier]] = None
    max_redemptions: Optional[int] = Field(None, ge=1)
    inventory_threshold: Optional[int] = Field(None, ge=0)
    is_active: Optional[bool] = None
    ends_at: Optional[datetime] = None
    priority: Optional[int] = Field(None, ge=0)


class ProductOfferOut(BaseModel):
    id: str
    tenant_id: str
    platform_id: Optional[str]
    product_name: Optional[str]
    offer_type: str
    offer_kind: str
    title: str
    description: Optional[str]
    discount_percent: Optional[float]
    discount_amount: Optional[float]
    combo_items: Optional[List[ComboItem]]
    combo_price: Optional[float]
    bulk_tiers: Optional[List[BulkTier]]
    max_redemptions: Optional[int]
    redemption_count: int
    inventory_threshold: Optional[int]
    discount_code: Optional[str]
    starts_at: Optional[datetime]
    ends_at: Optional[datetime]
    is_active: bool
    priority: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class OfferFromTextCreate(BaseModel):
    text: str = Field(..., min_length=3, max_length=2000)
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
