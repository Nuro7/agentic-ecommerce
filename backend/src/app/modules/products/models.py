import uuid
from datetime import datetime
from sqlalchemy import String, Integer, DateTime, func, ForeignKey, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column
from ...core.database import Base


class ProductCache(Base):
    """Cached product data pulled from the store platform."""
    __tablename__ = "product_cache"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id", ondelete="CASCADE"))
    platform_id: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    price: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    currency: Mapped[str] = mapped_column(String(10), default="USD")
    image_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    in_stock: Mapped[bool] = mapped_column(default=True)
    cached_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
