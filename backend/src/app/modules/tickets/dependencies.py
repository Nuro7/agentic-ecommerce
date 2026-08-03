from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.database import get_db
from .service import TicketService


async def get_ticket_service(db: AsyncSession = Depends(get_db)) -> TicketService:
    return TicketService(db)
