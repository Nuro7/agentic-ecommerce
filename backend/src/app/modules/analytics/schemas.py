from pydantic import BaseModel
from datetime import datetime


class MetricOut(BaseModel):
    date: datetime
    total_conversations: int
    completed_purchases: int
    revenue: float
    avg_session_seconds: int

    model_config = {"from_attributes": True}


class AnalyticsSummary(BaseModel):
    total_conversations: int
    completed_purchases: int
    total_revenue: float
    conversion_rate: float
