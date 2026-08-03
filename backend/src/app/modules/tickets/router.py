"""Merchant dashboard — voice ticket endpoints.

Authed with the merchant JWT (get_authenticated_tenant). The optional `shop`
query param matches the widget's shop=domain identity and is cross-checked
against the authenticated tenant so one merchant can never read another's
tickets. When omitted, the authenticated tenant's own tickets are returned.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.database import get_db
from ..tenants.dependencies import get_authenticated_tenant
from ..tenants.repository import TenantRepository
from .dependencies import get_ticket_service
from .schemas import TicketOut, TicketUpdate
from .service import TicketService

router = APIRouter(prefix="/merchant/tickets", tags=["tickets"])


class TicketListOut(BaseModel):
    tickets: List[TicketOut]
    total: int
    status: Optional[str] = None


async def _resolve_tenant_id(tenant, shop: Optional[str], db: AsyncSession) -> str:
    """Authenticated tenant wins; an optional ?shop= must resolve to the SAME tenant."""
    if shop and str(shop).strip():
        domain = str(shop).strip().rstrip("/")
        resolved = await TenantRepository(db).get_by_shopify_domain(domain)
        if not resolved or resolved.id != tenant.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="shop does not belong to the authenticated merchant",
            )
    return tenant.id


@router.get("", response_model=TicketListOut)
async def list_tickets(
    shop: Optional[str] = Query(None, description="Shopify domain, e.g. mystore.myshopify.com"),
    status_filter: Optional[str] = Query(
        None, alias="status", description="Filter by open | in_progress | resolved"
    ),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    tenant=Depends(get_authenticated_tenant),
    db: AsyncSession = Depends(get_db),
    service: TicketService = Depends(get_ticket_service),
):
    tenant_id = await _resolve_tenant_id(tenant, shop, db)
    if status_filter not in (None, "", "open", "in_progress", "resolved"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="status must be one of open | in_progress | resolved",
        )
    tickets = await service.list_tickets(
        tenant_id, status=status_filter or None, limit=limit, offset=offset
    )
    total = await service.count_tickets(tenant_id, status=status_filter or None)
    return TicketListOut(tickets=tickets, total=total, status=status_filter or None)


@router.patch("/{ticket_id}", response_model=TicketOut)
async def update_ticket(
    ticket_id: str,
    body: TicketUpdate,
    shop: Optional[str] = Query(None),
    tenant=Depends(get_authenticated_tenant),
    db: AsyncSession = Depends(get_db),
    service: TicketService = Depends(get_ticket_service),
):
    tenant_id = await _resolve_tenant_id(tenant, shop, db)
    updated = await service.update_status(ticket_id, tenant_id, body.status)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    return updated
