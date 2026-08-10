"""Unit tests for natural-language offer parsing (modules/offers/from_text.py).

The LLM path is mocked to None so the deterministic regex fallback is what
gets exercised — the endpoint must work offline.
"""
import pytest

from src.app.modules.offers.from_text import (
    _parse_amount,
    _parse_bulk_tiers,
    _parse_combo,
    _parse_percent,
    parse_offer_from_text,
)


class TestParsers:
    def test_percent(self):
        assert _parse_percent("20% off Nike shoes") == 20.0
        assert _parse_percent("flat 10.5% discount") == 10.5
        assert _parse_percent("no discount here") is None

    def test_amount(self):
        assert _parse_amount("flat 500 off") == 500.0
        assert _parse_amount("Rs. 1,250 off") == 1250.0
        assert _parse_amount("no amount") is None

    def test_bulk_tiers(self):
        tiers = _parse_bulk_tiers("2+ => 10%, 3+ => 20%")
        assert tiers == [
            {"min_qty": 2, "discount_percent": 10.0},
            {"min_qty": 3, "discount_percent": 20.0},
        ]
        tiers2 = _parse_bulk_tiers("min 2 qty for 15%")
        assert tiers2 == [{"min_qty": 2, "discount_percent": 15.0}]
        assert _parse_bulk_tiers("just a sale") is None

    def test_combo(self):
        combo = _parse_combo("buy 1 get 1 free on Watches")
        assert combo is not None
        assert combo["items"][0]["quantity"] == 1
        assert combo["items"][0]["name"] == "Watches"
        combo2 = _parse_combo("buy Shoe + Watch at 999")
        assert combo2 is not None
        assert combo2["price"] == 999.0
        assert _parse_combo("nothing here") is None


@pytest.mark.parametrize("text,kwargs", [
    ("20% off Nike shoes", {"offer_kind": "discount", "discount_percent": 20.0}),
    ("flat 500 off on T-shirts", {"offer_kind": "discount", "discount_amount": 500.0}),
    ("2+ => 10%, 3+ => 20% on Pens", {"offer_kind": "bulk"}),
])
class TestParseOffer:
    async def test_parse(self, text, kwargs):
        data = await parse_offer_from_text(text)
        for key, value in kwargs.items():
            assert data.get(key) == value, f"{key}: {data.get(key)} != {value}"
        assert data["title"]
        assert data["offer_kind"] in ("discount", "dead_stock", "combo", "bulk")

    async def test_never_raises(self, text, kwargs):
        data = await parse_offer_from_text(text)
        assert isinstance(data, dict)

    async def test_schema_valid(self, text, kwargs):
        from src.app.modules.offers.schemas import ProductOfferCreate
        data = await parse_offer_from_text(text)
        ProductOfferCreate.model_validate(data)
