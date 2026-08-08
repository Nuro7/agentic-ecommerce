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


# ── Merchant dashboard — GET /api/v1/merchant/dashboard/metrics ──────────

class KpiRevenueOut(BaseModel):
    total_amount: float
    lift_percentage: float
    share_of_sales_percentage: float


class KpiConversionsOut(BaseModel):
    order_count: int
    lift_percentage: float
    avg_order_value: float


class KpiEngagementOut(BaseModel):
    voice_split_percentage: float
    text_split_percentage: float
    avg_voice_turn_seconds: int


class KpiPlanUsageOut(BaseModel):
    credits_used: int
    credits_total: int
    plan_tier: str


class DashboardKpisOut(BaseModel):
    revenue: KpiRevenueOut
    conversions: KpiConversionsOut
    engagement: KpiEngagementOut
    plan_usage: KpiPlanUsageOut


class TrendPointOut(BaseModel):
    date: str
    sales: float
    turns: int


class LivePulseHubOut(BaseModel):
    active_shoppers: int
    voice_streams: int
    managed_carts: int
    pending_checkouts: int
    status_message: str


class PerformanceHubOut(BaseModel):
    revenue_dialogue_trend: list[TrendPointOut]
    live_pulse: LivePulseHubOut


class TopConvertedProductOut(BaseModel):
    product_name: str
    promo_badge: str | None
    qty_sold: int
    revenue: float


class RecentTicketOut(BaseModel):
    ticket_id: str | None
    customer_name: str | None
    issue_type: str | None
    heat_rating: str | None
    heat_color_hex: str | None
    transcript_snippet: str


class SupportEscalationsOut(BaseModel):
    auto_resolved_percentage: float
    escalated_percentage: float
    recent_tickets: list[RecentTicketOut]


class OperationalDeskOut(BaseModel):
    top_converted_products: list[TopConvertedProductOut]
    support_escalations: SupportEscalationsOut


class DashboardDataOut(BaseModel):
    kpis: DashboardKpisOut
    performance_hub: PerformanceHubOut
    operational_desk: OperationalDeskOut


class DashboardMetricsOut(BaseModel):
    status: str
    data: DashboardDataOut
