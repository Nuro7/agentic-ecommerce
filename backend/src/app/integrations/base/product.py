"""Shared product data model returned by all integrations."""
from dataclasses import dataclass, field


@dataclass
class Product:
    id: str
    name: str
    price: float
    currency: str = "USD"
    description: str = ""
    image_url: str = ""
    in_stock: bool = True
    variant_id: str | None = None
    url: str = ""
    tags: list[str] = field(default_factory=list)
