"""Seed the 3 initial Razorpay plans (INR).

Idempotent — safe to run multiple times (uses ON CONFLICT DO UPDATE).

Usage:
    # From repo root with .env populated:
    python scripts/seed_plans.py
"""

import asyncio
import sys
from pathlib import Path

# Allow running from repo root without installing the package.
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv  # type: ignore[import]

load_dotenv()

from sqlalchemy import text

from app.infrastructure.persistence.database import db_session


PLANS = [
    {
        "code": "starter_monthly_inr",
        "name": "Starter",
        "description": "For small stores getting started with AI commerce.",
        "marketing_tagline": "Try the agent on your store",
        "price_paise": 10_000,   # INR 100
        "currency": "INR",
        "interval": "monthly",
        "trial_days": 14,
        "gateway": "razorpay",
        "is_custom": False,
        "is_active": True,
        "is_publicly_listed": True,
        "display_order": 10,
        "has_overage_pricing": False,
        "overage_rates": {},
        "metadata": {"highlight_color": "#94a3b8", "recommended_for": "small stores"},
    },
    {
        "code": "growth_monthly_inr",
        "name": "Growth",
        "description": "For stores scaling their conversational commerce.",
        "marketing_tagline": "Most popular",
        "price_paise": 20_000,   # INR 200
        "currency": "INR",
        "interval": "monthly",
        "trial_days": 14,
        "gateway": "razorpay",
        "is_custom": False,
        "is_active": True,
        "is_publicly_listed": True,
        "display_order": 20,
        "has_overage_pricing": True,
        "overage_rates": {"sessions.monthly": {"per_unit_paise": 50}},
        "metadata": {"highlight_color": "#3b82f6", "is_recommended": True},
    },
    {
        "code": "scale_monthly_inr",
        "name": "Scale",
        "description": "For high-volume stores with custom branding needs.",
        "marketing_tagline": "For ambitious teams",
        "price_paise": 30_000,   # INR 300
        "currency": "INR",
        "interval": "monthly",
        "trial_days": 14,
        "gateway": "razorpay",
        "is_custom": False,
        "is_active": True,
        "is_publicly_listed": True,
        "display_order": 30,
        "has_overage_pricing": True,
        "overage_rates": {
            "sessions.monthly": {"per_unit_paise": 25},
            "voice_minutes.monthly": {"per_unit_paise": 100},
        },
        "metadata": {"highlight_color": "#8b5cf6"},
    },
]


async def seed() -> None:
    import json

    print("Seeding initial plans...")

    upsert_sql = text("""
        INSERT INTO plans (
            code, name, description, marketing_tagline,
            price_paise, currency, interval, trial_days,
            gateway, is_custom, is_active, is_publicly_listed,
            display_order, has_overage_pricing, overage_rates, metadata
        ) VALUES (
            :code, :name, :description, :marketing_tagline,
            :price_paise, :currency, :interval, :trial_days,
            :gateway, :is_custom, :is_active, :is_publicly_listed,
            :display_order, :has_overage_pricing,
            CAST(:overage_rates AS jsonb), CAST(:metadata AS jsonb)
        )
        ON CONFLICT (code) DO UPDATE SET
            name             = EXCLUDED.name,
            description      = EXCLUDED.description,
            marketing_tagline = EXCLUDED.marketing_tagline,
            price_paise      = EXCLUDED.price_paise,
            has_overage_pricing = EXCLUDED.has_overage_pricing,
            overage_rates    = EXCLUDED.overage_rates,
            metadata         = EXCLUDED.metadata,
            updated_at       = NOW()
    """)

    async with db_session() as session:
        for plan in PLANS:
            params = {**plan}
            params["overage_rates"] = json.dumps(plan["overage_rates"])
            params["metadata"] = json.dumps(plan["metadata"])
            await session.execute(upsert_sql, params)

        await session.commit()
        print(f"  Upserted {len(PLANS)} plans.")

    print("Done.")


if __name__ == "__main__":
    asyncio.run(seed())
