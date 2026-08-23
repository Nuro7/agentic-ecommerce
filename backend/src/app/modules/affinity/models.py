"""Affinity model for product co-occurrence."""
import uuid
from datetime import datetime
from sqlalchemy import String, Integer, DateTime, ForeignKey, func, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from ...core.database import Base


class ProductAffinity(Base):
    __tablename__ = "product_affinity"
    __table_args__ = (
        UniqueConstraint("tenant_id", "product_id_a", "product_id_b", name="uq_affinity_tenant_pair"),
    )

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id_a: Mapped[str] = mapped_column(String(255), nullable=False)
    product_id_b: Mapped[str] = mapped_column(String(255), nullable=False)
    co_count: Mapped[int] = mapped_column(Integer, default=0)
    pair_support: Mapped[int] = mapped_column(Integer, default=0)
    last_computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())