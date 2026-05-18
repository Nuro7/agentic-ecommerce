from pydantic import BaseModel
from datetime import datetime


class ProductOut(BaseModel):
    id: str
    platform_id: str
    name: str
    description: str | None
    price: float
    currency: str
    image_url: str | None
    in_stock: bool

    model_config = {"from_attributes": True}


class ProductSearchRequest(BaseModel):
    query: str
    limit: int = 10
