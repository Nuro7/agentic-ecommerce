import uuid
from datetime import datetime
from sqlalchemy import String, DateTime, func, ForeignKey, Text, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from ...core.database import Base


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "dedup_key", name="uq_webhook_events_tenant_dedup"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id", ondelete="CASCADE"))
    topic: Mapped[str] = mapped_column(String(100))
    platform: Mapped[str] = mapped_column(String(50))
    payload: Mapped[str] = mapped_column(Text)
    # sha256(topic|payload) — unique per tenant so redelivered webhooks are skipped.
    dedup_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
