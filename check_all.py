from src.app.core.database import AsyncSessionLocal
from sqlalchemy import text
import asyncio

async def check():
    async with AsyncSessionLocal() as db:
        # Check all tenants
        result = await db.execute(text("SELECT id, name, shopify_domain FROM tenants"))
        for row in result:
            print(f'Tenant: {row.id} | Name: {row.name} | Domain: {row.shopify_domain}')
            
            # Check product cache for each
            result2 = await db.execute(text("SELECT COUNT(*) FROM product_cache WHERE tenant_id = :tid"), {"tid": row.id})
            count = result2.scalar()
            print(f'  Products in cache: {count}')
            if count > 0:
                result3 = await db.execute(text("SELECT name, platform_id FROM product_cache WHERE tenant_id = :tid LIMIT 3"), {"tid": row.id})
                for r in result3:
                    print(f'    {r.name} (platform_id: {r.platform_id})')

asyncio.run(check())