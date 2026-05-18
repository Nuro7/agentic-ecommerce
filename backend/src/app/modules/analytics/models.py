import uuid
from datetime import datetime
from sqlalchemy import String, Integer, DateTime, func, ForeignKey, Numeric
from sqlalchemy.orm import Mapped, mapped_column
from ...core.database import Base


class ConversationMetric(Base):
    __tablename__ = "conversation_metrics"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id", ondelete="CASCADE"))
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    total_conversations: Mapped[int] = mapped_column(Integer, default=0)
    completed_purchases: Mapped[int] = mapped_column(Integer, default=0)
    revenue: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    avg_session_seconds: Mapped[int] = mapped_column(Integer, default=0)
