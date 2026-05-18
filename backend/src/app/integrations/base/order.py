"""Shared order data model returned by all integrations."""
from dataclasses import dataclass


@dataclass
class Order:
    id: str
    status: str
    total: float
    currency: str = "USD"
    customer_email: str = ""
    tracking_url: str = ""
