from sqlalchemy.ext.asyncio import AsyncSession
from .repository import ProductRepository
from .models import ProductCache


class ProductService:
    def __init__(self, db: AsyncSession):
        self.repo = ProductRepository(db)

    async def search_products(self, tenant_id: str, query: str, limit: int = 10) -> list[ProductCache]:
        return await self.repo.search(tenant_id, query, limit)

    async def sync_product(self, tenant_id: str, platform_id: str, data: dict) -> ProductCache:
        product = ProductCache(
            tenant_id=tenant_id,
            platform_id=platform_id,
            name=data.get("name", ""),
            description=data.get("description"),
            price=float(data.get("price", 0)),
            currency=data.get("currency", "USD"),
            image_url=data.get("image_url"),
            in_stock=data.get("in_stock", True),
        )
        return await self.repo.upsert(product)
