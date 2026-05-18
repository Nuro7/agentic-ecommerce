from pydantic import BaseModel
from datetime import datetime


class PlanOut(BaseModel):
    id: str
    name: str
    price_monthly: float
    max_conversations: int
    max_stores: int

    model_config = {"from_attributes": True}


class SubscriptionOut(BaseModel):
    id: str
    tenant_id: str
    plan_id: str
    status: str
    current_period_start: datetime
    current_period_end: datetime

    model_config = {"from_attributes": True}


class UsageOut(BaseModel):
    metric: str
    value: int
    recorded_at: datetime

    model_config = {"from_attributes": True}
