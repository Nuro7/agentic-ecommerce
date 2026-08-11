from src.app.core.database import AsyncSessionLocal
from src.app.modules.tenants.models import Tenant
from sqlalchemy import select
import asyncio

async def check():
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Tenant))
        tenants = result.scalars().all()
        for t in tenants:
            print(f'Tenant: {t.id} | Domain: {t.shopify_domain} | Has Admin Token: {bool(t.shopify_access_token)} | Has Storefront Token: {bool(t.shopify_storefront_token)} | Active: {t.is_active}')

asyncio.run(check())