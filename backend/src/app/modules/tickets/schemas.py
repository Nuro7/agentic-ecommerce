from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .models import TICKET_PRIORITIES, TICKET_STATUSES


class TicketCreate(BaseModel):
    """Internal create payload — built by the agent, not the merchant."""
    shop_domain: Optional[str] = None
    session_id: str = Field(..., max_length=255)
    customer_name: Optional[str] = Field(None, max_length=255)
    customer_phone: Optional[str] = Field(None, max_length=50)
    customer_email: Optional[str] = Field(None, max_length=255)
    issue_summary: str = Field(..., max_length=4000)
    transcript_json: Optional[Dict[str, Any]] = None
    priority: str = Field(default="medium", pattern="|".join(TICKET_PRIORITIES))


class TicketUpdate(BaseModel):
    """Merchant dashboard — update ticket status."""
    status: str = Field(..., pattern="|".join(TICKET_STATUSES))


class TicketOut(BaseModel):
    id: str
    tenant_id: str
    shop_domain: Optional[str]
    session_id: str
    customer_name: Optional[str]
    customer_phone: Optional[str]
    customer_email: Optional[str]
    issue_summary: str
    transcript_json: Optional[Dict[str, Any]]
    priority: str
    status: str
    webhook_sent: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TranscriptTurn(BaseModel):
    role: str
    content: str
