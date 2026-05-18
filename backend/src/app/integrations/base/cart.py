"""Shared cart data models returned by all integrations."""
from dataclasses import dataclass, field


@dataclass
class CartItem:
    id: str
    product_id: str
    name: str
    quantity: int
    unit_price: float
    variant_id: str | None = None


@dataclass
class Cart:
    session_id: str
    items: list[CartItem] = field(default_factory=list)
    subtotal: float = 0.0
    checkout_url: str = ""
