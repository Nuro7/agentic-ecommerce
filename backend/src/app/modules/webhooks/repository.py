from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from .models import WebhookEvent


class WebhookRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, event: WebhookEvent) -> WebhookEvent:
        self.db.add(event)
        await self.db.commit()
        await self.db.refresh(event)
        return event

    async def list_pending(self) -> list[WebhookEvent]:
        result = await self.db.execute(
            select(WebhookEvent).where(WebhookEvent.status == "pending")
        )
        return list(result.scalars().all())

    async def mark_processed(self, event_id: str) -> None:
        from datetime import datetime, timezone
        from sqlalchemy import update
        await self.db.execute(
            update(WebhookEvent).where(WebhookEvent.id == event_id).values(
                status="processed",
                processed_at=datetime.now(timezone.utc),
            )
        )
        await self.db.commit()
