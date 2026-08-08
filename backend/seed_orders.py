"""Seed dummy orders (+ items) for a tenant so the merchant dashboard has data.

Run from the `backend/` directory (uses the same `src` import layout as
seed_tenant.py). DATABASE_URL must point at the target DB — on Render, run this
from the service Shell (the env var is already set there).

Usage:
    python seed_orders.py --tenant <tenant_id> [--orders 300] [--days 45]
    python seed_orders.py                  # lists active tenants and seeds the first

Idempotency: refuses to run if the tenant already has SEED-* orders unless
`--force` is passed.
"""
import argparse
import asyncio
import random
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from src.app.core.database import AsyncSessionLocal, set_request_tenant, set_tenant_guc
from src.app.modules.orders.models import Order, OrderItem
from src.app.modules.products.models import ProductCache
from src.app.modules.tenants.models import Tenant

CUSTOMER_EMAILS = [
    "mike.johnson@gmail.com", "sarah.davis@yahoo.com", "alex.miller@outlook.com",
    "emma.wilson@gmail.com", "james.taylor@gmail.com", "olivia.moore@icloud.com",
    "lucas.anderson@gmail.com", "mia.thomas@yahoo.com", "noah.jackson@gmail.com",
    "ava.white@outlook.com",
]

FALLBACK_ITEMS = [
    ("Wireless Headphones", "fal-headphones", 59.99),
    ("Leather Backpack", "fal-backpack", 89.00),
    ("Classic Sunglasses", "fal-sunglasses", 29.99),
    ("Ceramic Coffee Mug", "fal-mug", 14.50),
    ("Denim Jacket", "fal-jacket", 74.99),
]


def weighted_choice(pairs):
    r = random.random()
    acc = 0.0
    for value, weight in pairs:
        acc += weight
        if r <= acc:
            return value
    return pairs[-1][0]


async def resolve_tenant(session, tenant_id):
    if tenant_id:
        tenant = await session.get(Tenant, tenant_id)
        if not tenant:
            raise SystemExit(f"Tenant {tenant_id} not found.")
        return tenant
    result = await session.execute(
        select(Tenant).where(Tenant.is_active.is_(True)).order_by(Tenant.created_at).limit(10)
    )
    tenants = list(result.scalars().all())
    if not tenants:
        raise SystemExit("No active tenants in the database.")
    print("Active tenants (using the first one):")
    for t in tenants:
        print(f"  {t.id}  {t.name}  {t.shopify_domain or ''}")
    return tenants[0]


async def seed(tenant_id: str, order_count: int, days: int, force: bool):
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    tenant_id = tenant_id or ""

    async with AsyncSessionLocal() as session:
        await set_request_tenant(tenant_id)
        await set_tenant_guc(session, tenant_id)

        if not force:
            existing = await session.execute(
                select(func.count(Order.id)).where(
                    Order.tenant_id == tenant_id,
                    Order.platform_order_id.like("SEED-%"),
                )
            )
            if existing.scalar():
                raise SystemExit(
                    "This tenant already has SEED-* orders. Pass --force to append, "
                    "or delete them first (see instructions)."
                )

        products = list(
            (
                await session.execute(
                    select(ProductCache).where(ProductCache.tenant_id == tenant_id).limit(8)
                )
            ).scalars().all()
        )
        print(f"Using {len(products)} real product(s) for order items.")

        # Distribute orders across the window: weekdays get a mild boost so the
        # revenue timeseries looks organic instead of uniform.
        daily_weights = []
        for i in range(days):
            d = start + timedelta(days=i)
            daily_weights.append(random.randint(1, 5) + (2 if d.weekday() < 5 else 0))
        total_weight = sum(daily_weights)
        daily_counts = [int(order_count * w / total_weight) for w in daily_weights]
        daily_counts[-1] += order_count - sum(daily_counts)

        created = 0
        for i, count in enumerate(daily_counts):
            day = start + timedelta(days=i)
            for _ in range(count):
                created_at = day.replace(
                    hour=random.randint(8, 22), minute=random.randint(0, 59), second=random.randint(0, 59)
                )
                status = weighted_choice([("completed", 0.8), ("pending", 0.2)])
                source = "store"
                if status == "completed":
                    source = weighted_choice([("agent", 0.65), ("store", 0.35)])

                item_count = random.randint(1, 3)
                items = []
                total = 0.0
                used = random.sample(
                    products, min(item_count, len(products)) if products else item_count
                )
                for p in used if products else random.sample(FALLBACK_ITEMS, item_count):
                    if products:
                        name, product_id, sku, price = p.name, p.platform_id, p.platform_id, float(p.price or 0)
                        currency = p.currency or "USD"
                    else:
                        name, product_id, sku, price = p
                        currency = "USD"
                    if price <= 0:
                        price = round(random.uniform(15, 200), 2)
                    qty = random.randint(1, 3)
                    line_total = round(price * qty, 2)
                    total += line_total
                    items.append(
                        OrderItem(
                            id=str(uuid.uuid4()),
                            tenant_id=tenant_id,
                            order_id="",
                            product_id=product_id,
                            sku=sku,
                            name=name,
                            quantity=qty,
                            unit_price=price,
                            total=line_total,
                            currency=currency,
                        )
                    )
                if total <= 0:
                    total = round(random.uniform(20, 250), 2)

                order = Order(
                    id=str(uuid.uuid4()),
                    tenant_id=tenant_id,
                    platform_order_id=f"SEED-{created + 1:05d}",
                    source=source,
                    status=status,
                    total=total,
                    currency="USD",
                    customer_email=random.choice(CUSTOMER_EMAILS),
                    notes="Seed data — safe to delete",
                    created_at=created_at,
                )
                session.add(order)
                for it in items:
                    it.order_id = order.id
                    session.add(it)
                created += 1
                if created % 100 == 0:
                    await session.commit()
                    print(f"  {created} orders...")

        await session.commit()
        print(f"Done. Inserted {created} orders for tenant {tenant_id} across {days} day(s).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default=None, help="Tenant UUID (default: first active tenant)")
    parser.add_argument("--orders", type=int, default=300, help="Total orders to insert")
    parser.add_argument("--days", type=int, default=45, help="How many days back to spread them")
    parser.add_argument("--force", action="store_true", help="Append even if SEED-* orders exist")
    args = parser.parse_args()

    async def main():
        async with AsyncSessionLocal() as session:
            tenant = await resolve_tenant(session, args.tenant)
        await seed(tenant.id, args.orders, args.days, args.force)

    asyncio.run(main())
