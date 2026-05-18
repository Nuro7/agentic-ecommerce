import uuid
from datetime import datetime
from sqlalchemy import String, Integer, DateTime, func, ForeignKey, Numeric
from sqlalchemy.orm import Mapped, mapped_column
from ...core.database import Base


class CartItem(Base):
    __tablename__ = "cart_items"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id", ondelete="CASCADE"))
    session_id: Mapped[str] = mapped_column(String(255), index=True)
    platform_product_id: Mapped[str] = mapped_column(String(255))
    variant_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name: Mapped[str] = mapped_column(String(500))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_price: Mapped[float] = mapped_column(Numeric(10, 2))
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
