"""Voice-ticket service — persistence + async external-helpdesk webhook emitter."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Dict, List, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import VoiceTicket
from .repository import TicketRepository

logger = logging.getLogger(__name__)

# Fallback webhook endpoint used when the tenant has no per-shop webhook URL
# configured (e.g. self-hosted / single-tenant deployments). Prefer the tenant
# column (tenants.tickets_webhook_url) when present.
_ENV_WEBHOOK_FALLBACK = os.getenv("GORGIAS_WEBHOOK_URL", "").strip()

_WEBHOOK_TIMEOUT = 15.0


class TicketService:
    def __init__(self, db: AsyncSession):
        self.repo = TicketRepository(db)
        self.db = db

    # ── Create ────────────────────────────────────────────────────────────────

    async def create_ticket(self, tenant_id: str, data: dict) -> VoiceTicket:
        ticket = await self.repo.create(tenant_id, data)

        # Emit the external-helpdesk webhook asynchronously — never block the
        # agent turn on an outbound POST (a slow/misconfigured webhook must not
        # add latency or fail the ticket).
        webhook_url = await self._resolve_webhook_url(tenant_id)
        if webhook_url:
            asyncio.create_task(self._emit_ticket_created(webhook_url, ticket))

        return ticket

    # ── Read ──────────────────────────────────────────────────────────────────

    async def list_tickets(
        self,
        tenant_id: str,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[VoiceTicket]:
        return await self.repo.list_by_tenant(tenant_id, status=status, limit=limit, offset=offset)

    async def count_tickets(self, tenant_id: str, status: Optional[str] = None) -> int:
        return await self.repo.count_by_tenant(tenant_id, status=status)

    async def get_ticket(self, ticket_id: str, tenant_id: str) -> Optional[VoiceTicket]:
        return await self.repo.get_by_id(ticket_id, tenant_id)

    # ── Update ────────────────────────────────────────────────────────────────

    async def update_status(self, ticket_id: str, tenant_id: str, status: str) -> Optional[VoiceTicket]:
        ticket = await self.repo.get_by_id(ticket_id, tenant_id)
        if not ticket:
            return None
        return await self.repo.update(ticket, {"status": status})

    # ── Webhook helpers ───────────────────────────────────────────────────────

    async def _resolve_webhook_url(self, tenant_id: str) -> str:
        """Tenant-configured helpdesk webhook URL, else the env fallback."""
        try:
            from ...modules.tenants.models import Tenant

            result = await self.db.execute(
                select(Tenant.tickets_webhook_url).where(Tenant.id == tenant_id)
            )
            tenant_url = result.scalar_one_or_none()
            if tenant_url and str(tenant_url).strip():
                return str(tenant_url).strip()
        except Exception as exc:
            logger.debug("Webhook URL lookup failed tenant=%s: %s", tenant_id, exc)
        return _ENV_WEBHOOK_FALLBACK

    @staticmethod
    def _ticket_payload(ticket: VoiceTicket) -> Dict:
        return {
            "event": "ticket.created",
            "payload": {
                "id": ticket.id,
                "shop_domain": ticket.shop_domain,
                "session_id": ticket.session_id,
                "customer_name": ticket.customer_name,
                "customer_phone": ticket.customer_phone,
                "customer_email": ticket.customer_email,
                "issue_summary": ticket.issue_summary,
                "transcript": ticket.transcript_json,
                "priority": ticket.priority,
                "status": ticket.status,
                "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
            },
        }

    async def _emit_ticket_created(self, webhook_url: str, ticket: VoiceTicket) -> None:
        payload = self._ticket_payload(ticket)
        delivered = False
        try:
            async with httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT) as client:
                resp = await client.post(
                    webhook_url,
                    json=payload,
                    headers={"Content-Type": "application/json", "X-Speako-Event": "ticket.created"},
                )
                if resp.is_success:
                    delivered = True
                    logger.info(
                        "Helpdesk webhook delivered: ticket=%s status=%s",
                        ticket.id, resp.status_code,
                    )
                else:
                    logger.warning(
                        "Helpdesk webhook returned %s for ticket=%s body=%.200s",
                        resp.status_code, ticket.id, resp.text[:200],
                    )
        except Exception as exc:
            logger.warning("Helpdesk webhook failed for ticket=%s: %s", ticket.id, exc)

        if delivered:
            # Record delivery so the dashboard can show which tickets reached the
            # external helpdesk. Fresh session — the request-scoped one is gone.
            try:
                from ...core.database import AsyncSessionLocal
                from sqlalchemy import update as sa_update

                async with AsyncSessionLocal() as db:
                    await db.execute(
                        sa_update(VoiceTicket)
                        .where(VoiceTicket.id == ticket.id)
                        .values(webhook_sent=True)
                    )
                    await db.commit()
            except Exception as exc:
                logger.debug("webhook_sent flag update failed: %s", exc)
