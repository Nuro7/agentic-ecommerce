import uuid
from datetime import datetime
from sqlalchemy import String, Integer, Boolean, DateTime, func, ForeignKey, Numeric, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from ...core.database import Base


class ProductCache(Base):
    """Cached product data pulled from the store platform.

    Columns added across migrations:
      0001  base columns (id … cached_at)
      0005  embedding (vector), tags, category_slug, search_vector (tsvector)
      0006  stock_quantity, permalink  +  UNIQUE(tenant_id, platform_id)

    Note: embedding and search_vector use PostgreSQL-specific types (vector /
    tsvector) that SQLAlchemy core doesn't model natively.  They are declared
    as Text here so the ORM can read them as strings; writes go through raw
    SQL in the sync task to use the proper CAST(...) syntax.
    """
    __tablename__ = "product_cache"
    __table_args__ = (
        UniqueConstraint("tenant_id", "platform_id", name="uq_product_cache_tenant_platform"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id", ondelete="CASCADE"))
    platform_id: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    price: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    currency: Mapped[str] = mapped_column(String(10), default="USD")
    image_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    in_stock: Mapped[bool] = mapped_column(Boolean, default=True)
    cached_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Added in migration 0005
    tags: Mapped[str | None] = mapped_column(Text, nullable=True)
    category_slug: Mapped[str | None] = mapped_column(String(255), nullable=True)
    embedding: Mapped[str | None] = mapped_column(Text, nullable=True)       # vector(1536) on disk
    search_vector: Mapped[str | None] = mapped_column(Text, nullable=True)   # tsvector on disk

    # Added in migration 0006
    stock_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    permalink: Mapped[str | None] = mapped_column(Text, nullable=True)
