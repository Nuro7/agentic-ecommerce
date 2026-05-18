from pydantic import BaseModel
from datetime import datetime


class WebhookEventOut(BaseModel):
    id: str
    topic: str
    platform: str
    status: str
    attempts: int
    received_at: datetime

    model_config = {"from_attributes": True}
