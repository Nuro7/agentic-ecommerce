import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from sqlalchemy.dialects.postgresql import insert

from .models import ConversationMetric
from ..conversations.models import Conversation, Message
from ..offers.models import ProductOffer
from ..orders.models import Order, OrderItem
from ..tickets.models import VoiceTicket


class AnalyticsRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_metrics(self, tenant_id: str, from_date: datetime, to_date: datetime) -> list[ConversationMetric]:
        result = await self.db.execute(
            select(ConversationMetric).where(
                ConversationMetric.tenant_id == tenant_id,
                ConversationMetric.date >= from_date,
                ConversationMetric.date <= to_date,
            ).order_by(ConversationMetric.date)
        )
        return list(result.scalars().all())

    async def get_totals(self, tenant_id: str) -> dict:
        result = await self.db.execute(
            select(
                func.sum(ConversationMetric.total_conversations),
                func.sum(ConversationMetric.completed_purchases),
                func.sum(ConversationMetric.revenue),
            ).where(ConversationMetric.tenant_id == tenant_id)
        )
        row = result.one()
        return {
            "total_conversations": row[0] or 0,
            "completed_purchases": row[1] or 0,
            "total_revenue": float(row[2] or 0),
        }

    async def upsert_daily_metric(
        self,
        tenant_id: str,
        date: datetime,
        from_date: datetime,
        to_date: datetime,
    ) -> ConversationMetric:
        """Aggregate conversations + orders for [from_date, to_date) and upsert one metric row.

        Safe to re-run — if a row already exists for (tenant_id, date) it is
        updated in-place rather than creating a duplicate.
        """
        # Count conversations started in the window
        conv_result = await self.db.execute(
            select(func.count(Conversation.id)).where(
                and_(
                    Conversation.tenant_id == tenant_id,
                    Conversation.created_at >= from_date,
                    Conversation.created_at < to_date,
                )
            )
        )
        total_conversations: int = conv_result.scalar() or 0

        # Count completed orders + sum revenue
        order_result = await self.db.execute(
            select(
                func.count(Order.id),
                func.coalesce(func.sum(Order.total), 0),
            ).where(
                and_(
                    Order.tenant_id == tenant_id,
                    Order.status == "completed",
                    Order.created_at >= from_date,
                    Order.created_at < to_date,
                )
            )
        )
        order_row = order_result.one()
        completed_purchases: int = order_row[0] or 0
        revenue: Decimal = Decimal(str(order_row[1] or 0))

        # Upsert: insert or update on (tenant_id, date) conflict
        stmt = (
            insert(ConversationMetric)
            .values(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                date=date,
                total_conversations=total_conversations,
                completed_purchases=completed_purchases,
                revenue=revenue,
                avg_session_seconds=0,
            )
            .on_conflict_do_update(
                index_elements=["tenant_id", "date"],
                set_={
                    "total_conversations": total_conversations,
                    "completed_purchases": completed_purchases,
                    "revenue": revenue,
                },
            )
        )
        await self.db.execute(stmt)

        # Return the row for logging
        row_result = await self.db.execute(
            select(ConversationMetric).where(
                ConversationMetric.tenant_id == tenant_id,
                ConversationMetric.date == date,
            )
        )
        return row_result.scalar_one()

    async def get_agent_sales(
        self,
        tenant_id: str,
        from_date: datetime,
        to_date: datetime,
    ) -> dict:
        """Agent-attributed sales for [from_date, to_date) plus period-over-period
        growth vs. the immediately preceding window of equal length."""
        window = to_date - from_date
        prior_from = from_date - window
        prior_to = from_date

        async def _agg(start, end) -> dict:
            result = await self.db.execute(
                select(
                    func.count(Order.id),
                    func.coalesce(func.sum(Order.total), 0),
                ).where(
                    and_(
                        Order.tenant_id == tenant_id,
                        Order.status == "completed",
                        Order.source == "agent",
                        Order.created_at >= start,
                        Order.created_at < end,
                    )
                )
            )
            row = result.one()
            return {"count": row[0] or 0, "revenue": float(row[1] or 0)}

        # All completed orders in the window → share-of-revenue denominator.
        total_result = await self.db.execute(
            select(
                func.count(Order.id),
                func.coalesce(func.sum(Order.total), 0),
            ).where(
                and_(
                    Order.tenant_id == tenant_id,
                    Order.status == "completed",
                    Order.created_at >= from_date,
                    Order.created_at < to_date,
                )
            )
        )
        trow = total_result.one()
        total_count = trow[0] or 0
        total_revenue = float(trow[1] or 0)

        current = await _agg(from_date, to_date)
        prior = await _agg(prior_from, prior_to)
        return {
            "agent_revenue": current["revenue"],
            "agent_order_count": current["count"],
            "total_revenue": total_revenue,
            "total_order_count": total_count,
            "agent_aov": round(current["revenue"] / current["count"], 2) if current["count"] else 0.0,
            "share_of_revenue": round(current["revenue"] / total_revenue * 100, 2) if total_revenue else 0.0,
            "boost_percent": round(
                (current["revenue"] - prior["revenue"]) / prior["revenue"] * 100, 2
            ) if prior["revenue"] else 0.0,
        }

    # ── Merchant dashboard support queries (all tenant-scoped) ────────────────

    async def get_conversation_totals(self, tenant_id: str, start: datetime, end: datetime) -> dict:
        """Aggregate conversation_metrics rows in [start, end]."""
        result = await self.db.execute(
            select(
                func.coalesce(func.sum(ConversationMetric.total_conversations), 0),
                func.coalesce(func.sum(ConversationMetric.completed_purchases), 0),
                func.coalesce(func.avg(ConversationMetric.avg_session_seconds), 0),
            ).where(
                ConversationMetric.tenant_id == tenant_id,
                ConversationMetric.date >= start,
                ConversationMetric.date <= end,
            )
        )
        row = result.one()
        return {
            "conversations": int(row[0]),
            "purchases": int(row[1]),
            "avg_session_seconds": int(row[2]),
        }

    async def get_revenue_timeseries(self, tenant_id: str, start: datetime, end: datetime) -> dict:
        """Per-day maps of {date_str: value} for store revenue, Aria revenue, and turns."""
        total = await self.db.execute(
            select(func.date(Order.created_at), func.coalesce(func.sum(Order.total), 0))
            .where(
                Order.tenant_id == tenant_id,
                Order.status == "completed",
                Order.created_at >= start,
                Order.created_at < end,
            )
            .group_by(func.date(Order.created_at))
        )
        total_map = {str(d): float(v) for d, v in total.all()}
        agent = await self.db.execute(
            select(func.date(Order.created_at), func.coalesce(func.sum(Order.total), 0))
            .where(
                Order.tenant_id == tenant_id,
                Order.status == "completed",
                Order.source == "agent",
                Order.created_at >= start,
                Order.created_at < end,
            )
            .group_by(func.date(Order.created_at))
        )
        agent_map = {str(d): float(v) for d, v in agent.all()}
        turns = await self.db.execute(
            select(func.date(Message.created_at), func.count(Message.id))
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.tenant_id == tenant_id,
                Message.created_at >= start,
                Message.created_at < end,
            )
            .group_by(func.date(Message.created_at))
        )
        turns_map = {str(d): int(v) for d, v in turns.all()}
        return {"total": total_map, "agent": agent_map, "turns": turns_map}

    async def count_pending_checkouts(self, tenant_id: str) -> int:
        result = await self.db.execute(
            select(func.count(Order.id)).where(
                Order.tenant_id == tenant_id,
                Order.status == "pending",
            )
        )
        return int(result.scalar() or 0)

    async def get_offer_badge_map(self, tenant_id: str) -> dict:
        """{platform_id: offer title} for active offers so top-products can badge promos."""
        result = await self.db.execute(
            select(ProductOffer.platform_id, ProductOffer.title).where(
                ProductOffer.tenant_id == tenant_id,
                ProductOffer.is_active.is_(True),
            )
        )
        return {str(pid): title for pid, title in result.all()}

    async def list_recent_tickets(self, tenant_id: str, limit: int = 5) -> list[VoiceTicket]:
        result = await self.db.execute(
            select(VoiceTicket)
            .where(VoiceTicket.tenant_id == tenant_id)
            .order_by(VoiceTicket.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def count_tickets_in_window(self, tenant_id: str, start: datetime, end: datetime) -> int:
        result = await self.db.execute(
            select(func.count(VoiceTicket.id)).where(
                VoiceTicket.tenant_id == tenant_id,
                VoiceTicket.created_at >= start,
                VoiceTicket.created_at < end,
            )
        )
        return int(result.scalar() or 0)

    async def get_agent_products(
        self,
        tenant_id: str,
        from_date: datetime,
        to_date: datetime,
        limit: int = 20,
    ) -> list[dict]:
        """Top products sold via Speako, ranked by revenue."""
        result = await self.db.execute(
            select(
                OrderItem.product_id,
                OrderItem.name,
                func.sum(OrderItem.quantity),
                func.sum(OrderItem.total),
            )
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                and_(
                    Order.tenant_id == tenant_id,
                    Order.status == "completed",
                    Order.source == "agent",
                    Order.created_at >= from_date,
                    Order.created_at < to_date,
                )
            )
            .group_by(OrderItem.product_id, OrderItem.name)
            .order_by(func.sum(OrderItem.total).desc())
            .limit(limit)
        )
        return [
            {
                "product_id": row[0],
                "name": row[1],
                "quantity": int(row[2] or 0),
                "revenue": float(row[3] or 0),
            }
            for row in result.all()
        ]
