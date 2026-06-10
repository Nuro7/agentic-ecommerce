from pydantic import BaseModel, Field
from datetime import datetime


class AddToCartRequest(BaseModel):
    session_id: str
    platform_product_id: str
    variant_id: str | None = None
    name: str
    # Reject zero/negative quantities and absurd values (caused negative totals).
    quantity: int = Field(default=1, ge=1, le=999)
    unit_price: float = Field(ge=0)


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
