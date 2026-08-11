from src.app.core.database import AsyncSessionLocal
from sqlalchemy import text
import asyncio

async def check():
    async with AsyncSessionLocal() as db:
        result = await db.execute(text('SELECT DISTINCT tenant_id, COUNT(*) FROM product_cache GROUP BY tenant_id'))
        rows = result.fetchall()
        print("=== TENANTS IN CACHE ===")
        for row in rows:
            print(f"{row.tenant_id} | {row.count}")

asyncio.run(check())