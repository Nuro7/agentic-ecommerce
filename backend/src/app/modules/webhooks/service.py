import json
from sqlalchemy.ext.asyncio import AsyncSession
from .repository import WebhookRepository
from .models import WebhookEvent


class WebhookService:
    def __init__(self, db: AsyncSession):
        self.repo = WebhookRepository(db)

    async def ingest(self, tenant_id: str, topic: str, platform: str, payload: dict) -> WebhookEvent:
        event = WebhookEvent(
            tenant_id=tenant_id,
            topic=topic,
            platform=platform,
            payload=json.dumps(payload),
        )
        return await self.repo.create(event)

    async def process_pending(self) -> int:
        events = await self.repo.list_pending()
        for event in events:
            # TODO: dispatch to topic handlers (order.updated, product.updated, etc.)
            await self.repo.mark_processed(event.id)
        return len(events)
