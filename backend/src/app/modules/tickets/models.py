import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from ...core.database import Base

# Status + priority values the merchant dashboard surfaces / mutates.
TICKET_STATUSES = ("open", "in_progress", "resolved")
TICKET_PRIORITIES = ("low", "medium", "high")


class VoiceTicket(Base):
    """A support ticket auto-created by the voice/text assistant (Aria).

    Persisted whenever the customer asks for human help, a refund, damaged-order
    help, or hits an unresolvable catalog query. Carries the full voice transcript
    so a merchant support rep can replay the exact conversation.
    """

    __tablename__ = "voice_tickets"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)

    shop_domain: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    session_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # Customer context captured from the active session / address FSM.
    customer_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    customer_phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    customer_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # AI-generated summary of the problem + the full conversation turns.
    issue_summary: Mapped[str] = mapped_column(Text, nullable=False)
    transcript_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)

    priority: Mapped[str] = mapped_column(String(20), nullable=False, server_default="medium")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="open")

    # Structured classification (added 0022) so the merchant dashboard can group /
    # filter tickets without parsing free-text summaries.
    issue_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    order_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    product_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # Why the priority was assigned: "keyword:<token>" (deterministic) or "llm".
    priority_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Where the ticket came from: "llm" | "deterministic" (incl. intake flow).
    source: Mapped[str] = mapped_column(String(20), nullable=False, server_default="llm")

    # True when the ticket was also pushed to the merchant's external helpdesk
    # (Gorgias/Zendesk webhook) — for observability in the dashboard.
    webhook_sent: Mapped[bool] = mapped_column(server_default="false", default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
