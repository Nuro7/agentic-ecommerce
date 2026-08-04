from datetime import datetime
from typing import List, Optional

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import VoiceTicket


class TicketRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, tenant_id: str, data: dict) -> VoiceTicket:
        ticket = VoiceTicket(tenant_id=tenant_id, **data)
        self.db.add(ticket)
        await self.db.commit()
        await self.db.refresh(ticket)
        return ticket

    async def get_by_id(self, ticket_id: str, tenant_id: str) -> Optional[VoiceTicket]:
        stmt = select(VoiceTicket).where(
            and_(VoiceTicket.id == ticket_id, VoiceTicket.tenant_id == tenant_id)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def find_open_by_session(
        self,
        tenant_id: str,
        session_id: str,
        since: datetime,
    ) -> Optional[VoiceTicket]:
        """Most recent OPEN ticket for this tenant+session created at/after `since`.

        Used to de-duplicate escalations: the same customer complaining twice in
        the same session (within a short window) must not create two tickets.
        """
        stmt = (
            select(VoiceTicket)
            .where(
                and_(
                    VoiceTicket.tenant_id == tenant_id,
                    VoiceTicket.session_id == session_id,
                    VoiceTicket.status == "open",
                    VoiceTicket.created_at >= since,
                )
            )
            .order_by(VoiceTicket.created_at.desc())
            .limit(1)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_tenant(
        self,
        tenant_id: str,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[VoiceTicket]:
        stmt = select(VoiceTicket).where(VoiceTicket.tenant_id == tenant_id)
        if status:
            stmt = stmt.where(VoiceTicket.status == status)
        stmt = stmt.order_by(VoiceTicket.created_at.desc()).limit(limit).offset(offset)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def count_by_tenant(self, tenant_id: str, status: Optional[str] = None) -> int:
        from sqlalchemy import func

        stmt = select(func.count()).select_from(VoiceTicket).where(VoiceTicket.tenant_id == tenant_id)
        if status:
            stmt = stmt.where(VoiceTicket.status == status)
        result = await self.db.execute(stmt)
        return int(result.scalar() or 0)

    async def update(self, ticket: VoiceTicket, data: dict) -> VoiceTicket:
        for key, value in data.items():
            if value is not None:
                setattr(ticket, key, value)
        await self.db.commit()
        await self.db.refresh(ticket)
        return ticket
