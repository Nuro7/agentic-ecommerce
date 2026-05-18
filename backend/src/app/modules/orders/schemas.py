from pydantic import BaseModel
from datetime import datetime


class OrderOut(BaseModel):
    id: str
    session_id: str
    platform_order_id: str | None
    status: str
    total: float
    currency: str
    customer_email: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class OrderStatusUpdate(BaseModel):
    status: str
