import uuid
from datetime import datetime
from sqlalchemy import String, Integer, DateTime, func, ForeignKey, Numeric
from sqlalchemy.orm import Mapped, mapped_column
from ...core.database import Base


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(50), unique=True)
    price_monthly: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    max_conversations: Mapped[int] = mapped_column(Integer, default=500)
    max_stores: Mapped[int] = mapped_column(Integer, default=1)
    features: Mapped[str] = mapped_column(String, default="{}")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id", ondelete="CASCADE"), unique=True)
    plan_id: Mapped[str] = mapped_column(String, ForeignKey("plans.id"))
    status: Mapped[str] = mapped_column(String(50), default="active")
    current_period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UsageRecord(Base):
    __tablename__ = "usage_records"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id", ondelete="CASCADE"))
    metric: Mapped[str] = mapped_column(String(100))
    value: Mapped[int] = mapped_column(Integer, default=1)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
