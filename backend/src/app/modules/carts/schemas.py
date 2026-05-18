from pydantic import BaseModel
from datetime import datetime


class AddToCartRequest(BaseModel):
    session_id: str
    platform_product_id: str
    variant_id: str | None = None
    name: str
    quantity: int = 1
    unit_price: float


class CartItemOut(BaseModel):
    id: str
    platform_product_id: str
    variant_id: str | None
    name: str
    quantity: int
    unit_price: float

    model_config = {"from_attributes": True}


class CartOut(BaseModel):
    items: list[CartItemOut]
    total: float
