from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from .models import ProductCache


class ProductRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def upsert(self, product: ProductCache) -> ProductCache:
        existing = await self.db.execute(
            select(ProductCache).where(
                ProductCache.tenant_id == product.tenant_id,
                ProductCache.platform_id == product.platform_id,
            )
        )
        row = existing.scalar_one_or_none()
        if row:
            for field in ("name", "description", "price", "image_url", "in_stock"):
                setattr(row, field, getattr(product, field))
            await self.db.commit()
            return row
        self.db.add(product)
        await self.db.commit()
        await self.db.refresh(product)
        return product

    async def search(self, tenant_id: str, query: str, limit: int = 10) -> list[ProductCache]:
        result = await self.db.execute(
            select(ProductCache).where(
                ProductCache.tenant_id == tenant_id,
                ProductCache.name.ilike(f"%{query}%"),
            ).limit(limit)
        )
        return list(result.scalars().all())
