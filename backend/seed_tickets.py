"""Seed realistic voice_tickets for a tenant so the support desk looks lived-in.

Run from the `backend/` directory (same `src` import layout as seed_orders.py).
On the server:  docker compose -f infra/docker/docker-compose.prod.yml exec web \
                    python seed_tickets.py --tenant <tenant_id>

Usage:
    python seed_tickets.py --tenant <tenant_id> [--count 20] [--days 45]
    python seed_tickets.py                  # lists active tenants and seeds the first

Issue types / heat / priority match the real triage logic in
src/app/services/ticketing.py so the dashboard filters sort correctly.
Idempotent: refuses to run if the tenant already has TK-2xxx tickets unless
`--force` is passed.
"""
import argparse
import asyncio
import random
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from src.app.core.database import AsyncSessionLocal, set_request_tenant, set_tenant_guc
from src.app.modules.tickets.models import VoiceTicket
from src.app.modules.products.models import ProductCache
from src.app.modules.tenants.models import Tenant

SHOP_DOMAIN = "bigb-pisar0or.myshopify.com"

CUSTOMERS = [
    ("Rahul Sharma", "rahul.sharma@gmail.com", "+91 98111 22334"),
    ("Priya Patel", "priya.patel@yahoo.com", "+91 98222 33445"),
    ("Amit Verma", "amit.verma@gmail.com", "+91 98333 44556"),
    ("Sneha Iyer", "sneha.iyer@outlook.com", None),
    ("Vikram Singh", "vikram.singh@gmail.com", "+91 98444 55667"),
    ("Ananya Rao", "ananya.rao@gmail.com", None),
    ("Karan Mehta", "karan.mehta@yahoo.com", "+91 98555 66778"),
    ("Deepika Nair", None, None),
    ("Rohit Khanna", "rohit.khanna@gmail.com", "+91 98666 77889"),
    ("Fatima Khan", "fatima.khan@outlook.com", None),
    (None, None, None),
    ("Arjun Reddy", "arjun.reddy@gmail.com", "+91 98777 88990"),
]

# (customer, trigger text, issue_type, priority, heat, summary, assistant reply)
SCENARIOS = [
    (
        "I received my order but the shoes are damaged, the sole is cracked.",
        "damaged_order", "high", "warm",
        "Customer received order with a cracked sole on the right shoe. Asked for a replacement.",
        "I'm really sorry about the damaged pair. I can raise a replacement order for you — could you share your order number?",
    ),
    (
        "The wrong item was delivered, I ordered UK 8 but got UK 9.",
        "wrong_item", "high", "warm",
        "Wrong size delivered (UK 9 instead of UK 8). Exchange requested.",
        "My apologies for the mix-up! I'll start an exchange for the correct UK 8 size.",
    ),
    (
        "I want a refund for my order, it arrived defective.",
        "refund", "high", "warm",
        "Refund requested for a defective item that arrived damaged.",
        "I understand, let's get that refund processed. Could you share the order number and your email?",
    ),
    (
        "This is urgent! I was charged twice for my order.",
        "billing", "urgent", "hot",
        "Double charge on credit card. Customer is upset and wants immediate resolution.",
        "I completely understand the urgency — let me escalate this to billing right away.",
    ),
    (
        "Can I talk to a human agent please?",
        "talk_to_human", "low", "cold",
        "Customer requested to speak with a human support agent.",
        "Of course! I can connect you with our support team. I've raised a ticket for you.",
    ),
    (
        "Where is my order? It hasn't been delivered in 2 weeks.",
        "delivery_issue", "high", "warm",
        "Order not delivered after 2 weeks. Customer wants tracking and delivery date.",
        "I'm sorry for the delay. Let me check the tracking status and update you.",
    ),
    (
        "One item was missing from my delivery.",
        "missing_item", "high", "warm",
        "One item missing from a multi-item delivery. Replacement requested.",
        "That's frustrating — I'll arrange to ship the missing item right away.",
    ),
    (
        "I want to exchange these for a different size.",
        "exchange", "medium", "warm",
        "Exchange requested for a different size of the same shoe.",
        "Sure, I can set up an exchange. Which size would you like instead?",
    ),
    (
        "I was overcharged on my credit card.",
        "billing", "high", "warm",
        "Customer believes they were overcharged. Billing investigation needed.",
        "Let me check your invoice and compare the charged amount.",
    ),
    (
        "Do you have this shoe in blue colour?",
        "other", "low", "cold",
        "Product availability question about colour variants.",
        "Let me check our inventory for the blue colour variant.",
    ),
    (
        "I need to cancel my order before it ships.",
        "other", "medium", "cold",
        "Cancellation requested before shipment.",
        "I can help cancel the order if it hasn't shipped yet.",
    ),
    (
        "This is a scam! I'm contacting my lawyer.",
        "other", "urgent", "hot",
        "Customer accused the store of fraud and threatened legal action. Escalated.",
        "I'm very sorry for your experience. I'm escalating this to a senior agent immediately.",
    ),
    (
        "The delivery man stole my package.",
        "delivery_issue", "urgent", "hot",
        "Package reported stolen during delivery. Urgent investigation.",
        "That's very concerning — I'm flagging this as urgent and will investigate the delivery.",
    ),
    (
        "How do I return a pair of shoes I bought?",
        "exchange", "medium", "warm",
        "Return instructions requested for purchased shoes.",
        "You can return within 30 days. I can generate a return label for you.",
    ),
    (
        "My order status says delivered but I didn't get it.",
        "delivery_issue", "high", "hot",
        "Order marked delivered but not received. Delivery discrepancy.",
        "I understand — let me open a delivery investigation for you.",
    ),
    (
        "I want to speak to customer care about my bill.",
        "talk_to_human", "low", "cold",
        "Customer requested to speak to support about billing.",
        "I'll connect you with our billing team — a ticket has been created.",
    ),
    (
        "The shoes I received have a torn strap.",
        "damaged_order", "high", "warm",
        "Damaged strap on received shoes. Replacement requested.",
        "I'm sorry about the torn strap. I'll arrange a replacement pair.",
    ),
    (
        "I was told the sale price but charged full price.",
        "billing", "high", "hot",
        "Charged full price instead of advertised sale price.",
        "Let me verify the promotion and adjust the charge if needed.",
    ),
    (
        "Can someone call me back about my delivery?",
        "talk_to_human", "low", "cold",
        "Customer requested a callback regarding delivery.",
        "Of course — a support agent will call you back shortly.",
    ),
    (
        "The coupon code didn't work at checkout.",
        "other", "medium", "cold",
        "Coupon code failed at checkout.",
        "Let me check the coupon and re-apply it for you.",
    ),
]


def _pick_status(i: int) -> str:
    r = random.random()
    if r < 0.15:
        return "resolved"
    if r < 0.30:
        return "in_progress"
    return "open"


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


async def seed(tenant_id: str, count: int, days: int, force: bool):
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    tenant_id = tenant_id or ""

    async with AsyncSessionLocal() as session:
        set_request_tenant(tenant_id)
        await set_tenant_guc(session, tenant_id)

        if not force:
            existing = await session.execute(
                select(func.count(VoiceTicket.id)).where(
                    VoiceTicket.tenant_id == tenant_id,
                    VoiceTicket.ticket_number.like("TK-2%"),
                )
            )
            if existing.scalar():
                raise SystemExit(
                    "This tenant already has TK-2xxx tickets. Pass --force to append."
                )

        products = list(
            (
                await session.execute(
                    select(ProductCache).where(ProductCache.tenant_id == tenant_id).limit(6)
                )
            ).scalars().all()
        )

        created = 0
        for i in range(count):
            scenario = SCENARIOS[i % len(SCENARIOS)]
            trigger, issue_type, priority, heat, summary, assistant_reply = scenario
            customer = random.choice(CUSTOMERS)
            name, email, phone = customer

            created_at = start + timedelta(
                days=random.randint(0, days - 1),
                hours=random.randint(8, 22),
                minutes=random.randint(0, 59),
            )
            product = products[i % len(products)] if products else None
            product_id = product.platform_id if product else None

            ticket = VoiceTicket(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                shop_domain=SHOP_DOMAIN,
                session_id=f"seed-ticket-{created + 1}",
                customer_name=name,
                customer_phone=phone,
                customer_email=email,
                issue_summary=summary,
                transcript_json={
                    "turns": [
                        {"role": "user", "content": trigger},
                        {"role": "assistant", "content": assistant_reply},
                    ]
                },
                priority=priority,
                status=_pick_status(i),
                issue_type=issue_type,
                order_id=f"SEED-{random.randint(1, 300):05d}",
                product_id=product_id,
                priority_reason=f"keyword:{issue_type.replace('_', ' ')}",
                source="llm" if issue_type != "talk_to_human" else "deterministic",
                ticket_number=f"TK-{2001 + i:04d}",
                heat=heat,
                merchant_notes="Seed demo ticket — safe to delete." if random.random() < 0.3 else None,
                webhook_sent=random.random() < 0.4,
                created_at=created_at,
            )
            session.add(ticket)
            created += 1

        await session.commit()
        print(f"Done. Inserted {created} tickets for tenant {tenant_id} across {days} day(s).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default=None, help="Tenant UUID (default: first active tenant)")
    parser.add_argument("--count", type=int, default=20, help="Number of tickets to insert")
    parser.add_argument("--days", type=int, default=45, help="How many days back to spread them")
    parser.add_argument("--force", action="store_true", help="Append even if TK-2xxx tickets exist")
    args = parser.parse_args()

    async def main():
        async with AsyncSessionLocal() as session:
            tenant = await resolve_tenant(session, args.tenant)
        await seed(tenant.id, args.count, args.days, args.force)

    asyncio.run(main())
