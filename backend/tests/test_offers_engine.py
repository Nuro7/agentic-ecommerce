"""Unit tests for the deterministic offer rule engine (modules/offers/engine.py)."""
import pytest

from src.app.modules.offers.engine import (
    evaluate_cart,
    evaluate_combos,
    evaluate_bulk_tiers,
    evaluate_discounts,
    offer_to_dict,
)


def _cart(*lines):
    return list(lines)


def _line(pid, qty, price, name="P"):
    return {"platform_product_id": str(pid), "quantity": qty, "unit_price": price, "name": name}


def _offer(**kw):
    data = {
        "id": kw.get("id", "o1"),
        "platform_id": kw.get("platform_id", "1"),
        "offer_kind": kw.get("offer_kind", "discount"),
        "title": kw.get("title", "Test offer"),
        "discount_percent": kw.get("discount_percent"),
        "discount_amount": kw.get("discount_amount"),
        "combo_items": kw.get("combo_items"),
        "combo_price": kw.get("combo_price"),
        "bulk_tiers": kw.get("bulk_tiers"),
        "max_redemptions": kw.get("max_redemptions"),
        "redemption_count": kw.get("redemption_count", 0),
        "inventory_threshold": kw.get("inventory_threshold"),
        "discount_code": kw.get("discount_code"),
        "offer_type": kw.get("offer_type", "promotion"),
        "product_name": kw.get("product_name"),
        "starts_at": None,
        "ends_at": None,
        "is_active": True,
        "priority": 0,
    }
    return {k: v for k, v in data.items() if v is not None or k in (
        "id", "platform_id", "offer_kind", "title", "redemption_count", "is_active", "priority")}


class TestSimpleDiscounts:
    def test_percent_discount(self):
        cart = _cart(_line(1, 2, 100))
        offers = [_offer(id="a", platform_id="1", discount_percent=20)]
        res = evaluate_discounts(cart, offers)
        assert len(res) == 1
        assert res[0]["discount"] == 40.0  # 2 * 100 * 20%
        assert res[0]["kind"] == "discount"

    def test_amount_discount_capped_at_line_total(self):
        cart = _cart(_line(1, 1, 50))
        offers = [_offer(id="a", platform_id="1", discount_amount=500)]
        res = evaluate_discounts(cart, offers)
        assert res[0]["discount"] == 50.0

    def test_offer_for_different_product_not_applied(self):
        cart = _cart(_line(2, 1, 50))
        offers = [_offer(id="a", platform_id="1", discount_percent=10)]
        assert evaluate_discounts(cart, offers) == []

    def test_exhausted_offer_skipped(self):
        cart = _cart(_line(1, 1, 100))
        offers = [_offer(id="a", platform_id="1", discount_percent=10,
                         max_redemptions=3, redemption_count=3)]
        assert evaluate_discounts(cart, offers) == []


class TestCombo:
    def test_satisfied_combo_applies_bundle_price(self):
        cart = _cart(_line(1, 1, 100), _line(2, 2, 50))
        offer = _offer(
            id="c1", offer_kind="combo", platform_id="1",
            combo_items=[
                {"platform_id": "1", "quantity": 1, "name": "Shoe"},
                {"platform_id": "2", "quantity": 2, "name": "Watch"},
            ],
            combo_price=120,
        )
        res = evaluate_combos(cart, [offer])
        assert len(res) == 1
        assert res[0]["full_price"] == 200.0   # 100 + 2*50
        assert res[0]["bundle_price"] == 120.0
        assert res[0]["savings"] == 80.0

    def test_unsatisfied_combo_not_applied(self):
        cart = _cart(_line(1, 1, 100))
        offer = _offer(
            id="c1", offer_kind="combo",
            combo_items=[
                {"platform_id": "1", "quantity": 1, "name": "Shoe"},
                {"platform_id": "2", "quantity": 1, "name": "Watch"},
            ],
            combo_price=120,
        )
        assert evaluate_combos(cart, [offer]) == []

    def test_exhausted_combo_skipped(self):
        cart = _cart(_line(1, 1, 100), _line(2, 1, 50))
        offer = _offer(
            id="c1", offer_kind="combo", max_redemptions=1, redemption_count=1,
            combo_items=[{"platform_id": "1", "quantity": 1}, {"platform_id": "2", "quantity": 1}],
            combo_price=100,
        )
        assert evaluate_combos(cart, [offer]) == []


class TestBulk:
    def test_highest_applicable_tier(self):
        cart = _cart(_line(1, 3, 100))
        offer = _offer(
            id="b1", offer_kind="bulk", platform_id="1",
            bulk_tiers=[
                {"min_qty": 2, "discount_percent": 10.0},
                {"min_qty": 3, "discount_percent": 20.0},
            ],
        )
        res = evaluate_bulk_tiers(cart, [offer])
        assert len(res) == 1
        assert res[0]["discount"] == 60.0  # 3 * 100 * 20%

    def test_tier_not_reached(self):
        cart = _cart(_line(1, 1, 100))
        offer = _offer(id="b1", offer_kind="bulk", platform_id="1",
                       bulk_tiers=[{"min_qty": 2, "discount_percent": 10.0}])
        assert evaluate_bulk_tiers(cart, [offer]) == []

    def test_amount_tier(self):
        cart = _cart(_line(1, 2, 100))
        offer = _offer(id="b1", offer_kind="bulk", platform_id="1",
                       bulk_tiers=[{"min_qty": 2, "discount_amount": 25.0}])
        res = evaluate_bulk_tiers(cart, [offer])
        assert res[0]["discount"] == 50.0


class TestEvaluateCart:
    def test_no_offers(self):
        cart = _cart(_line(1, 2, 100))
        res = evaluate_cart(cart, [])
        assert res["subtotal"] == 200.0
        assert res["savings"] == 0.0
        assert res["total"] == 200.0
        assert res["applied_offers"] == []

    def test_combo_excludes_discount_on_same_line(self):
        # Line 1 is part of an applied combo → the simple 50% discount on line 1
        # must NOT stack on top of the bundle price.
        cart = _cart(_line(1, 1, 100), _line(2, 1, 50))
        combo = _offer(
            id="c1", offer_kind="combo",
            combo_items=[{"platform_id": "1", "quantity": 1}, {"platform_id": "2", "quantity": 1}],
            combo_price=100,
        )
        simple = _offer(id="d1", platform_id="1", discount_percent=50)
        res = evaluate_cart(cart, [combo, simple])
        # bundle full price 150, bundle price 100 → savings 50; no extra 50% off line 1
        assert res["savings"] == 50.0
        assert res["total"] == 100.0

    def test_bulk_and_discount_stack(self):
        cart = _cart(_line(1, 3, 100), _line(2, 1, 50))
        bulk = _offer(id="b1", offer_kind="bulk", platform_id="1",
                      bulk_tiers=[{"min_qty": 2, "discount_percent": 10.0}])
        simple = _offer(id="d1", platform_id="2", discount_percent=20)
        res = evaluate_cart(cart, [bulk, simple])
        assert res["savings"] == 40.0  # 30 (bulk) + 10 (20% of 50)
        assert res["total"] == 310.0

    def test_total_never_negative(self):
        cart = _cart(_line(1, 1, 10))
        offer = _offer(id="d1", platform_id="1", discount_amount=999)
        res = evaluate_cart(cart, [offer])
        assert res["total"] == 0.0

    def test_applied_offer_ids(self):
        cart = _cart(_line(1, 1, 100), _line(2, 1, 50))
        offers = [
            _offer(id="c1", offer_kind="combo",
                   combo_items=[{"platform_id": "1", "quantity": 1},
                                {"platform_id": "2", "quantity": 1}],
                   combo_price=100),
            _offer(id="d1", platform_id="9", discount_percent=10),
        ]
        res = evaluate_cart(cart, offers)
        assert res["applied_offers"] == ["c1"]


class TestOfferToDict:
    def test_passthrough_dict(self):
        d = {"id": "x"}
        assert offer_to_dict(d) is d

    def test_orm_like_object(self):
        class Fake:
            id = "f1"
            platform_id = "1"
            product_name = None
            offer_type = "promotion"
            offer_kind = "bulk"
            title = "T"
            description = None
            discount_percent = None
            discount_amount = None
            combo_items = None
            combo_price = None
            bulk_tiers = [{"min_qty": 2, "discount_percent": 5}]
            max_redemptions = None
            redemption_count = 0
            inventory_threshold = None
            discount_code = None
            starts_at = None
            ends_at = None
            is_active = True
            priority = 0

        out = offer_to_dict(Fake())
        assert out["id"] == "f1"
        assert out["bulk_tiers"] == [{"min_qty": 2, "discount_percent": 5}]
