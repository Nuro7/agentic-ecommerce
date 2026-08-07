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


class AgentSalesSummary(BaseModel):
    """Attribution summary for the merchant "products sold via Speako" view."""

    agent_revenue: float
    agent_order_count: int
    agent_aov: float
    total_revenue: float
    total_order_count: int
    share_of_revenue: float
    boost_percent: float


class AgentProductOut(BaseModel):
    product_id: str | None
    name: str
    quantity: int
    revenue: float
