"""Affinity repository — top-N complement queries + cold-start fallback + bestsellers."""
from typing import List, Optional
from sqlalchemy import select, func, and_, or_, text
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta, timezone
from .models import ProductAffinity
from ..products.models import ProductCache
from ..orders.models import Order, OrderItem


class AffinityRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_top_complements(
        self,
        tenant_id: str,
        cart_product_ids: List[str],
        exclude_ids: List[str],
        limit: int = 10,
        min_support: int = 2,
    ) -> List[dict]:
        if not cart_product_ids:
            return []

        conditions = []
        for pid in cart_product_ids:
            conditions.append(
                or_(
                    ProductAffinity.product_id_a == pid,
                    ProductAffinity.product_id_b == pid,
                )
            )

        stmt = (
            select(
                ProductAffinity.product_id_a,
                ProductAffinity.product_id_b,
                ProductAffinity.co_count,
            )
            .where(
                and_(
                    ProductAffinity.tenant_id == tenant_id,
                    or_(*conditions),
                    ProductAffinity.co_count >= min_support,
                )
            )
            .order_by(ProductAffinity.co_count.desc())
            .limit(limit * 3)
        )
        result = await self.db.execute(stmt)
        rows = result.all()

        complements: dict[str, int] = {}
        for a, b, co in rows:
            other = b if a in cart_product_ids else a
            if other not in cart_product_ids and other not in exclude_ids:
                complements[other] = complements.get(other, 0) + co

        sorted_complements = sorted(complements.items(), key=lambda x: x[1], reverse=True)
        return [{"platform_id": pid, "co_count": co} for pid, co in sorted_complements[:limit]]

    async def get_category_cooccurrence_fallback(
        self,
        tenant_id: str,
        cart_product_ids: List[str],
        exclude_ids: List[str],
        limit: int = 10,
    ) -> List[dict]:
        if not cart_product_ids:
            return []

        stmt = select(ProductCache.category_slug, ProductCache.tags).where(
            and_(
                ProductCache.tenant_id == tenant_id,
                ProductCache.platform_id.in_(cart_product_ids),
            )
        )
        result = await self.db.execute(stmt)
        rows = result.all()

        categories = set()
        tags = set()
        for cat, tag_str in rows:
            if cat:
                categories.add(cat)
            if tag_str:
                tags.update(t.strip() for t in tag_str.split(",") if t.strip())

        if not categories and not tags:
            return []

        conditions = []
        if categories:
            conditions.append(ProductCache.category_slug.in_(categories))
        if tags:
            for tag in list(tags)[:10]:
                conditions.append(ProductCache.tags.ilike(f"%{tag}%"))

        stmt = (
            select(ProductCache.platform_id, ProductCache.name, ProductCache.price, ProductCache.category_slug)
            .where(
                and_(
                    ProductCache.tenant_id == tenant_id,
                    ProductCache.platform_id.notin_(cart_product_ids + exclude_ids),
                    ProductCache.in_stock.is_(True),
                    or_(*conditions),
                )
            )
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return [
            {"platform_id": pid, "name": name, "price": float(price or 0), "category_slug": cat}
            for pid, name, price, cat in result.all()
        ]

    async def get_bestsellers(
        self,
        tenant_id: str,
        limit: int = 10,
        since: Optional[datetime] = None,
    ) -> List[dict]:
        if since is None:
            since = datetime.now(timezone.utc) - timedelta(days=90)

        stmt = (
            select(
                OrderItem.product_id,
                func.sum(OrderItem.quantity).label("total_qty"),
                func.sum(OrderItem.total).label("total_revenue"),
            )
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                and_(
                    Order.tenant_id == tenant_id,
                    Order.status == "completed",
                    Order.created_at >= since,
                    OrderItem.product_id.is_not(None),
                )
            )
            .group_by(OrderItem.product_id)
            .order_by(func.sum(OrderItem.total).desc())
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return [
            {"platform_id": pid, "total_qty": int(qty), "total_revenue": float(rev)}
            for pid, qty, rev in result.all()
        ]