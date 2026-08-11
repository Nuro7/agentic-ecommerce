from src.app.core.database import AsyncSessionLocal
from sqlalchemy import text
import asyncio

async def check():
    async with AsyncSessionLocal() as db:
        result = await db.execute(text('SELECT DISTINCT tenant_id, COUNT(*) FROM product_cache GROUP BY tenant_id'))
        for row in result:
            print(f'Tenant: {row.tenant_id} | Products: {row.count}')
            
        result = await db.execute(text("SELECT tenant_id, platform_id, name FROM product_cache WHERE name ILIKE '%BATA%' LIMIT 10"))
        for row in result:
            print(f'  Tenant: {row.tenant_id} | ID: {row.platform_id} | {row.name}')

asyncio.run(check())