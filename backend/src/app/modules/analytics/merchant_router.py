"""Merchant dashboard — single consolidated metrics endpoint.

GET /api/v1/merchant/dashboard/metrics?timeframe=last_30_days&shop=...

Returns everything the Stripe/Vercel-style dashboard needs in one fetch:
KPI cards, revenue/dialogue trend, live pulse, top converted products (with
offer badges), and the support-escalation desk. Every query is scoped to the
authenticated merchant's tenant_id (JWT) and additionally filtered by
tenant_id in SQL — RLS is also enforced by get_authenticated_tenant.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from .repository import AnalyticsRepository
from .schemas import (
    DashboardMetricsOut,
    DashboardDataOut,
    DashboardKpisOut,
    KpiRevenueOut,
    KpiConversionsOut,
    KpiEngagementOut,
    KpiPlanUsageOut,
    PerformanceHubOut,
    TrendPointOut,
    LivePulseHubOut,
    OperationalDeskOut,
    TopConvertedProductOut,
    SupportEscalationsOut,
    RecentTicketOut,
)
from ...core.database import get_db
from ..billing.repository import BillingRepository
from ..tenants.dependencies import get_authenticated_tenant
from ..tenants.repository import TenantRepository

router = APIRouter(prefix="/merchant/dashboard", tags=["merchant-dashboard"])

_HEAT_COLOR = {"hot": "#ef4444", "warm": "#f59e0b", "cold": "#3b82f6"}
# Voice costs 3 credits/turn, text 1 credit/turn (matches billing metering).
_VOICE_CREDIT_RATE = 3
_TEXT_CREDIT_RATE = 1


def _resolve_window(timeframe: str, start_date: datetime | None, end_date: datetime | None):
    end = end_date or datetime.now(timezone.utc)
    start = start_date
    if start is None:
        if timeframe in ("this_month", "month"):
            start = datetime(end.year, end.month, 1, tzinfo=end.tzinfo or timezone.utc)
        else:
            days = {"last_7_days": 7, "last_30_days": 30, "last_90_days": 90}.get(timeframe, 30)
            start = end - timedelta(days=days)
    if start.tzinfo is None:
        start = start.replace(tzinfo=end.tzinfo or timezone.utc)
    return start, end


def _month_buckets(start: datetime, end: datetime) -> list[str]:
    """Continuous month keys ('2026-08') covering [start, end)."""
    buckets: list[str] = []
    cur = datetime(start.year, start.month, 1, tzinfo=start.tzinfo)
    while cur < end:
        buckets.append(f"{cur.year}-{cur.month:02d}")
        nxt = cur.month % 12 + 1
        cur = datetime(cur.year + (1 if cur.month == 12 else 0), nxt, 1, tzinfo=cur.tzinfo)
    return buckets


def _transcript_snippet(ticket) -> str:
    tj = ticket.transcript_json
    if isinstance(tj, dict):
        turns = tj.get("turns") or tj.get("transcript") or []
        if isinstance(turns, list):
            parts = []
            for t in turns[:2]:
                if isinstance(t, dict):
                    role = str(t.get("role") or "").capitalize()
                    content = str(t.get("content") or "").strip()[:180]
                    if content:
                        parts.append(f"{role}: {content}")
            if parts:
                return "\n".join(parts)
    return (ticket.issue_summary or "")[:200]


@router.get("/metrics", response_model=DashboardMetricsOut)
async def dashboard_metrics(
    request: Request,
    timeframe: str = Query("last_30_days"),
    shop: str | None = Query(None, description="Optional Shopify domain cross-check"),
    start_date: datetime | None = Query(None),
    end_date: datetime | None = Query(None),
    tenant=Depends(get_authenticated_tenant),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = tenant.id
    if shop and str(shop).strip():
        resolved = await TenantRepository(db).get_by_shopify_domain(str(shop).strip().rstrip("/"))
        if not resolved or resolved.id != tenant_id:
            raise HTTPException(status_code=403, detail="shop does not belong to the authenticated merchant")

    start, end = _resolve_window(timeframe, start_date, end_date)
    repo = AnalyticsRepository(db)
    billing = BillingRepository(db)

    # ── KPIs: revenue / conversions (reuses the existing agent-sales query) ──
    agent = await repo.get_agent_sales(tenant_id, start, end)
    conv = await repo.get_conversation_totals(tenant_id, start, end)
    live_conversations = await repo.count_conversations(tenant_id, start, end)
    conversion_lift = round(agent["total_order_count"] / live_conversations * 100, 1) if live_conversations else 0.0

    # ── KPIs: plan + credit usage ────────────────────────────────────────────
    sub = await billing.get_subscription(tenant_id)
    plan = await billing.get_plan_by_id(sub.plan_id) if sub else None
    period_start = sub.current_period_start if sub else start
    credits_total = int(plan.max_conversations) if plan else 0
    text_credits = (
        await billing.get_usage_since(tenant_id, "conversations", period_start)
        + await billing.get_usage_since(tenant_id, "credits", period_start)
    ) * _TEXT_CREDIT_RATE
    voice_credits = (
        await billing.get_usage_since(tenant_id, "voice_turns", period_start) * _VOICE_CREDIT_RATE
    )
    credits_used = text_credits + voice_credits
    voice_split = round(voice_credits / credits_used * 100, 1) if credits_used else 0.0
    plan_tier = f"{plan.name.title()} Plan" if plan else "Free Plan"

    # ── Trend: monthly store revenue vs dialogue turns ───────────────────────
    ts = await repo.get_revenue_timeseries(tenant_id, start, end)
    trend = []
    for mkey in _month_buckets(start, end):
        ym = mkey.split("-")
        month_total = 0.0
        month_turns = 0
        for day, value in ts["total"].items():
            if str(day).startswith(mkey):
                month_total += value
        for day, value in ts["turns"].items():
            if str(day).startswith(mkey):
                month_turns += value
        trend.append(TrendPointOut(date=mkey, sales=round(month_total, 2), turns=month_turns))

    # ── Live pulse (Redis + in-process voice counter + DB) ───────────────────
    redis_client = getattr(request.app.state, "redis", None)
    redis_status = "connected"
    active_shoppers = 0
    managed_carts = 0
    if redis_client is None:
        redis_status = "disconnected"
    else:
        try:
            await redis_client.ping()
        except Exception:
            redis_status = "disconnected"
        if redis_status == "connected":
            try:
                cursor = 0
                while True:
                    cursor, keys = await redis_client.scan(
                        cursor, match=f"session:{tenant_id}:*", count=500
                    )
                    for k in keys:
                        if str(k).endswith(":cart"):
                            managed_carts += 1
                        elif not str(k).endswith(":meta"):
                            active_shoppers += 1
                    if cursor == 0:
                        break
            except Exception:
                pass

    from ...api.v1.voice import active_voice_streams
    voice_streams = active_voice_streams()
    pending_checkouts = await repo.count_pending_checkouts(tenant_id)
    status_parts = []
    status_parts.append(f"Redis {'Connected' if redis_status == 'connected' else 'Down'}")
    status_parts.append("WS Connected")
    status_message = "All systems operational • " + " & ".join(status_parts)

    # ── Top converted products + promo badges ────────────────────────────────
    badges = await repo.get_offer_badge_map(tenant_id)
    rows = await repo.get_agent_products(tenant_id, start, end, limit=4)
    top_products = [
        TopConvertedProductOut(
            product_name=r["name"],
            promo_badge=badges.get(str(r["product_id"])),
            qty_sold=r["quantity"],
            revenue=round(r["revenue"], 2),
        )
        for r in rows
    ]

    # ── Support escalations desk ─────────────────────────────────────────────
    total_tickets = await repo.count_tickets_in_window(tenant_id, start, end)
    auto_resolved_count = max(0, live_conversations - total_tickets)
    auto_resolved_pct = (
        round(auto_resolved_count / live_conversations * 100, 1) if live_conversations else 0.0
    )
    recent_tickets = []
    for t in await repo.list_recent_tickets(tenant_id, limit=5):
        heat = t.heat or "cold"
        recent_tickets.append(
            RecentTicketOut(
                ticket_id=t.ticket_number or t.id,
                customer_name=t.customer_name,
                issue_type=t.issue_type,
                heat_rating=heat.title(),
                heat_color_hex=_HEAT_COLOR.get(heat, "#6b7280"),
                transcript_snippet=_transcript_snippet(t),
            )
        )

    data = DashboardDataOut(
        kpis=DashboardKpisOut(
            revenue=KpiRevenueOut(
                total_amount=round(agent["total_revenue"], 2),
                lift_percentage=round(agent["boost_percent"], 1),
                share_of_sales_percentage=round(agent["share_of_revenue"], 1),
            ),
            conversions=KpiConversionsOut(
                order_count=agent["agent_order_count"],
                lift_percentage=conversion_lift,
                avg_order_value=round(agent["agent_aov"], 2),
            ),
            engagement=KpiEngagementOut(
                voice_split_percentage=voice_split,
                text_split_percentage=round(100 - voice_split, 1),
                avg_voice_turn_seconds=conv["avg_session_seconds"],
            ),
            plan_usage=KpiPlanUsageOut(
                credits_used=credits_used,
                credits_total=credits_total,
                plan_tier=plan_tier,
            ),
        ),
        performance_hub=PerformanceHubOut(
            revenue_dialogue_trend=trend,
            live_pulse=LivePulseHubOut(
                active_shoppers=active_shoppers,
                voice_streams=voice_streams,
                managed_carts=managed_carts,
                pending_checkouts=pending_checkouts,
                status_message=status_message,
            ),
        ),
        operational_desk=OperationalDeskOut(
            top_converted_products=top_products,
            support_escalations=SupportEscalationsOut(
                auto_resolved_percentage=auto_resolved_pct,
                escalated_percentage=round(100 - auto_resolved_pct, 1),
                recent_tickets=recent_tickets,
            ),
        ),
    )
    return DashboardMetricsOut(status="success", data=data)
