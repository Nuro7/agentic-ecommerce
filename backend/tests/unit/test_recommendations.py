"""Unit tests for the recommendation engine (modules/offers/recommendations.py).

Covers the cart-aware suggestion generators (combo / bulk / dead-stock) and the
two async entry points (get_promoted_products_for_prompt and
get_recommendations_for_cart) with mocked repos + store clients — pure unit,
no database, so it runs in ms.
"""
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.app.modules.offers.recommendations import (
    _product_brief,
    _combo_suggestions,
    _bulk_suggestions,
    _dead_stock_suggestions,
    get_promoted_products_for_prompt,
    get_recommendations_for_cart,
)


def _line(pid, qty, price=None, name="P"):
    return {"platform_product_id": str(pid), "quantity": qty, "unit_price": price or 10.0, "name": name}


def _offer(**kw):
    data = {
        "id": kw.get("id", "o1"),
        "platform_id": kw.get("platform_id", "1"),
        "product_name": kw.get("product_name", "Product"),
        "offer_type": kw.get("offer_type", "promotion"),
        "offer_kind": kw.get("offer_kind", "discount"),
        "title": kw.get("title", "Offer"),
        "discount_percent": kw.get("discount_percent"),
        "discount_amount": kw.get("discount_amount"),
        "combo_items": kw.get("combo_items"),
        "combo_price": kw.get("combo_price"),
        "bulk_tiers": kw.get("bulk_tiers"),
        "max_redemptions": kw.get("max_redemptions"),
        "redemption_count": kw.get("redemption_count", 0),
        "inventory_threshold": kw.get("inventory_threshold"),
        "discount_code": kw.get("discount_code"),
    }
    return {k: v for k, v in data.items() if v is not None}


def _orm_offer(**kw):
    """An object exposing attrs like a ProductOffer ORM row."""
    return SimpleNamespace(**{
        "id": kw.get("id", "o1"),
        "platform_id": kw.get("platform_id", "1"),
        "product_name": kw.get("product_name", "Product"),
        "offer_type": kw.get("offer_type", "promotion"),
        "offer_kind": kw.get("offer_kind", "discount"),
        "title": kw.get("title", "Offer"),
        "description": kw.get("description"),
        "discount_percent": kw.get("discount_percent"),
        "discount_amount": kw.get("discount_amount"),
        "combo_items": kw.get("combo_items"),
        "combo_price": kw.get("combo_price"),
        "bulk_tiers": kw.get("bulk_tiers"),
        "max_redemptions": kw.get("max_redemptions"),
        "redemption_count": kw.get("redemption_count", 0),
        "inventory_threshold": kw.get("inventory_threshold"),
        "discount_code": kw.get("discount_code"),
        "starts_at": None,
        "ends_at": None,
        "is_active": True,
        "priority": 0,
    })


class FakeDB:
    """Minimal stand-in for the async SQLAlchemy session — only close() is used."""
    closed = False

    async def close(self):
        self.closed = True


class FakeStore:
    """configurable async store client."""
    def __init__(self, details=None, error=None):
        self.details = details or {}
        self.error = error

    async def get_product_details(self, platform_id):
        if self.error:
            raise self.error
        return dict(self.details)


# ── _combo_suggestions ────────────────────────────────────────────────────────

class TestComboSuggestions:
    def test_offer_without_combo_items_skipped(self):
        offers = [_offer(id="a", offer_kind="combo")]
        assert _combo_suggestions(offers, []) == []

    def test_empty_combo_items_list_skipped(self):
        offers = [_offer(id="a", offer_kind="combo", combo_items=[])]
        assert _combo_suggestions(offers, []) == []

    def test_satisfied_combo_no_missing(self):
        offers = [_offer(
            id="a", offer_kind="combo", platform_id="1",
            combo_price=120,
            combo_items=[
                {"platform_id": "1", "quantity": 1, "name": "Shoe"},
                {"platform_id": "2", "quantity": 2, "name": "Watch"},
            ],
        )]
        cart = [_line(1, 1), _line(2, 2)]
        out = _combo_suggestions(offers, cart)
        assert len(out) == 1
        assert out[0]["satisfied"] is True
        assert out[0]["missing_items"] == []
        assert out[0]["bundle_price"] == 120.0

    def test_over_fulfilled_combo_is_satisfied(self):
        offers = [_offer(
            id="a", offer_kind="combo",
            combo_items=[{"platform_id": "1", "quantity": 1, "name": "Shoe"}],
        )]
        cart = [_line(1, 5)]
        out = _combo_suggestions(offers, cart)
        assert out[0]["satisfied"] is True
        assert out[0]["missing_items"] == []

    def test_partial_combo_reports_missing_quantities(self):
        offers = [_offer(
            id="a", offer_kind="combo",
            combo_items=[
                {"platform_id": "1", "quantity": 1, "name": "Shoe"},
                {"platform_id": "2", "quantity": 3, "name": "Watch"},
            ],
        )]
        cart = [_line(1, 1), _line(2, 1)]  # needs 3 watches, has 1
        out = _combo_suggestions(offers, cart)
        assert out[0]["satisfied"] is False
        assert out[0]["missing_items"] == [
            {"platform_id": "2", "name": "Watch", "quantity": 2}
        ]

    def test_pid_type_normalization_int_vs_str(self):
        offers = [_offer(
            id="a", offer_kind="combo",
            combo_items=[{"platform_id": "101", "quantity": 1, "name": "Shoe"}],
        )]
        cart = [{"platform_product_id": 101, "quantity": 2, "unit_price": 10.0}]  # int pid
        assert _combo_suggestions(offers, cart)[0]["satisfied"] is True

    def test_multiple_cart_lines_same_pid_summed(self):
        offers = [_offer(
            id="a", offer_kind="combo",
            combo_items=[{"platform_id": "1", "quantity": 3, "name": "Shoe"}],
        )]
        cart = [_line(1, 2), _line(1, 1)]
        assert _combo_suggestions(offers, cart)[0]["satisfied"] is True

    def test_zero_quantity_in_combo_item_defaults_to_one(self):
        offers = [_offer(
            id="a", offer_kind="combo",
            combo_items=[{"platform_id": "1", "quantity": 0, "name": "Shoe"}],
        )]
        cart = [_line(1, 0)]
        out = _combo_suggestions(offers, cart)
        # need defaults to 1, have is 0 → missing 1
        assert out[0]["satisfied"] is False
        assert out[0]["missing_items"][0]["quantity"] == 1


# ── _bulk_suggestions ─────────────────────────────────────────────────────────

class TestBulkSuggestions:
    def test_no_tiers_skipped(self):
        assert _bulk_suggestions([_offer(id="a", offer_kind="bulk")], []) == []

    def test_below_tier_returns_add_quantity(self):
        offers = [_offer(
            id="a", offer_kind="bulk", platform_id="1",
            bulk_tiers=[{"min_qty": 3, "discount_percent": 10.0}],
        )]
        cart = [_line(1, 1)]
        out = _bulk_suggestions(offers, cart)
        assert len(out) == 1
        assert out[0]["kind"] == "bulk"
        assert out[0]["current_qty"] == 1
        assert out[0]["add_quantity"] == 2

    def test_tier_already_reached_no_suggestion(self):
        offers = [_offer(
            id="a", offer_kind="bulk", platform_id="1",
            bulk_tiers=[{"min_qty": 3, "discount_percent": 10.0}],
        )]
        cart = [_line(1, 4)]
        assert _bulk_suggestions(offers, cart) == []

    def test_only_lowest_unmet_tier_suggested(self):
        offers = [_offer(
            id="a", offer_kind="bulk", platform_id="1",
            bulk_tiers=[
                {"min_qty": 2, "discount_percent": 5.0},
                {"min_qty": 5, "discount_percent": 20.0},
            ],
        )]
        cart = [_line(1, 3)]
        out = _bulk_suggestions(offers, cart)
        assert len(out) == 1
        assert out[0]["next_tier"]["min_qty"] == 5
        assert out[0]["add_quantity"] == 2

    def test_quantity_summed_across_duplicate_lines(self):
        offers = [_offer(
            id="a", offer_kind="bulk", platform_id="1",
            bulk_tiers=[{"min_qty": 3, "discount_percent": 10.0}],
        )]
        cart = [_line(1, 2), _line(1, 2)]
        assert _bulk_suggestions(offers, cart) == []  # 4 >= 3

    def test_missing_target_platform_assumes_zero(self):
        offers = [_offer(
            id="a", offer_kind="bulk", platform_id=None,  # no target product
            bulk_tiers=[{"min_qty": 2, "discount_percent": 10.0}],
        )]
        out = _bulk_suggestions(offers, [_line(1, 5)])
        assert out[0]["current_qty"] == 0
        assert out[0]["add_quantity"] == 2

    def test_int_qty_string_tier(self):
        offers = [_offer(
            id="a", offer_kind="bulk", platform_id="1",
            bulk_tiers=[{"min_qty": "2", "discount_percent": 10.0}],
        )]
        assert _bulk_suggestions(offers, [_line(1, "2")]) == []  # coercible to int == 2


# ── _dead_stock_suggestions ───────────────────────────────────────────────────

class TestDeadStockSuggestions:
    def test_minimal_fields(self):
        offers = [_offer(id="a", offer_kind="dead_stock", platform_id="7",
                         product_name="Old item", discount_percent=40)]
        out = _dead_stock_suggestions(offers, [])
        assert out[0]["kind"] == "dead_stock"
        assert out[0]["platform_id"] == "7"
        assert out[0]["name"] == "Old item"
        assert out[0]["discount_percent"] == 40
        assert out[0]["in_cart"] is False

    def test_offer_without_platform_id_skipped(self):
        assert _dead_stock_suggestions(
            [_offer(id="a", offer_kind="dead_stock", platform_id=None)], []) == []

    def test_in_cart_detection(self):
        offers = [_offer(id="a", offer_kind="dead_stock", platform_id="9")]
        cart = [{"platform_product_id": 9, "quantity": 1}]  # int pid
        assert _dead_stock_suggestions(offers, cart)[0]["in_cart"] is True


# ── _product_brief ────────────────────────────────────────────────────────────

class TestProductBrief:
    @pytest.mark.asyncio
    async def test_no_store_client_returns_empty(self):
        out = await _product_brief(None, 101)
        assert out == {"platform_id": "101", "name": "", "price": ""}

    @pytest.mark.asyncio
    async def test_uses_price_fallback_to_regular_price(self):
        store = FakeStore(details={"name": "Shoe", "regular_price": 99.5})
        out = await _product_brief(store, 101)
        assert out["name"] == "Shoe"
        assert out["price"] == 99.5

    @pytest.mark.asyncio
    async def test_none_price_becomes_zero(self):
        store = FakeStore(details={"name": "X"})
        out = await _product_brief(store, 101)
        assert out["price"] == 0.0

    @pytest.mark.asyncio
    async def test_store_client_error_is_swallowed(self):
        store = FakeStore(error=RuntimeError("boom"))
        out = await _product_brief(store, 101)
        assert out == {"platform_id": "101", "name": "", "price": ""}

    @pytest.mark.asyncio
    async def test_pid_normalized_to_string(self):
        store = FakeStore(details={"name": "X", "price": "42"})
        assert (await _product_brief(store, 202))["platform_id"] == "202"


# ── get_promoted_products_for_prompt ──────────────────────────────────────────

class TestPromotedProductsForPrompt:
    @pytest.mark.asyncio
    async def test_no_tenant_id(self):
        assert await get_promoted_products_for_prompt("", None, None) == []

    @pytest.mark.asyncio
    async def test_no_db_session_factory(self):
        assert await get_promoted_products_for_prompt("t1", None, None) == []

    @pytest.mark.asyncio
    async def test_no_offers_returns_empty(self):
        repo = AsyncMock()
        repo.get_active_promotions = AsyncMock(return_value=[])
        with patch("src.app.modules.offers.recommendations.OfferRepository",
                   return_value=repo):
            db = FakeDB()
            out = await get_promoted_products_for_prompt(
                "t1", FakeStore(), lambda: db, limit=5
            )
        assert out == []
        assert db.closed is True

    @pytest.mark.asyncio
    async def test_happy_path_with_store_details(self):
        repo = AsyncMock()
        offer = _orm_offer(id="p1", platform_id="7", product_name="Fallback name",
                           title="Big Sale", discount_percent=25)
        repo.get_active_promotions = AsyncMock(return_value=[offer])
        store = FakeStore(details={"name": "Live name", "price": 499.0})
        with patch("src.app.modules.offers.recommendations.OfferRepository",
                   return_value=repo):
            out = await get_promoted_products_for_prompt(
                "t1", store, lambda: FakeDB(), limit=5
            )
        assert out == [{
            "name": "Live name",
            "price": "₹499.0",
            "offer_title": "Big Sale",
            "discount_percent": 25,
            "discount_amount": None,
            "offer_type": "promotion",
            "offer_kind": "discount",
            "platform_id": "7",
        }]

    @pytest.mark.asyncio
    async def test_store_error_uses_product_name_fallback(self):
        repo = AsyncMock()
        offer = _orm_offer(id="p1", platform_id="7", product_name="Fallback name",
                           title="Dead stock", offer_kind="dead_stock")
        repo.get_active_promotions = AsyncMock(return_value=[offer])
        store = FakeStore(error=RuntimeError("store down"))
        with patch("src.app.modules.offers.recommendations.OfferRepository",
                   return_value=repo):
            out = await get_promoted_products_for_prompt(
                "t1", store, lambda: FakeDB(), limit=5
            )
        assert out[0]["name"] == "Fallback name"
        assert out[0]["price"] == ""

    @pytest.mark.asyncio
    async def test_limit_respected(self):
        repo = AsyncMock()
        offers = [_orm_offer(id=f"p{i}", platform_id=str(i), product_name=f"n{i}")
                  for i in range(10)]
        repo.get_active_promotions = AsyncMock(
            side_effect=lambda tenant_id, limit=5: offers[:limit]
        )
        store = FakeStore(details={"name": "X", "price": 5.0})
        with patch("src.app.modules.offers.recommendations.OfferRepository",
                   return_value=repo):
            out = await get_promoted_products_for_prompt(
                "t1", store, lambda: FakeDB(), limit=3
            )
        repo.get_active_promotions.assert_awaited_once_with("t1", limit=3)
        assert len(out) == 3

    @pytest.mark.asyncio
    async def test_repo_error_returns_empty(self):
        repo = AsyncMock()
        repo.get_active_promotions = AsyncMock(side_effect=RuntimeError("db down"))
        with patch("src.app.modules.offers.recommendations.OfferRepository",
                   return_value=repo):
            out = await get_promoted_products_for_prompt(
                "t1", FakeStore(), lambda: FakeDB(), limit=5
            )
        assert out == []


# ── get_recommendations_for_cart ──────────────────────────────────────────────

class TestRecommendationsForCart:
    @pytest.mark.asyncio
    async def test_missing_tenant_or_db_returns_empty(self):
        assert await get_recommendations_for_cart("", []) == []
        assert await get_recommendations_for_cart("t1", []) == []

    @pytest.mark.asyncio
    async def test_no_offers_returns_empty(self):
        repo = AsyncMock()
        repo.get_active_offers = AsyncMock(return_value=[])
        with patch("src.app.modules.offers.recommendations.OfferRepository",
                   return_value=repo):
            out = await get_recommendations_for_cart("t1", [_line(1, 1)],
                                                     db_session_factory=lambda: FakeDB())
        assert out == []

    @pytest.mark.asyncio
    async def test_ranking_satisfied_combo_first(self):
        satisfied = _offer(
            id="sc", offer_kind="combo", platform_id="1",
            combo_items=[{"platform_id": "1", "quantity": 1, "name": "Shoe"}],
        )
        partial = _offer(
            id="pc", offer_kind="combo", platform_id="2",
            combo_items=[{"platform_id": "2", "quantity": 2, "name": "Watch"}],
        )
        bulk = _offer(id="bu", offer_kind="bulk", platform_id="3",
                      bulk_tiers=[{"min_qty": 3, "discount_percent": 10}])
        dead = _offer(id="ds", offer_kind="dead_stock", platform_id="4")
        cart = [_line(1, 1), _line(2, 1), _line(3, 1)]  # line 2 short of qty 2

        repo = AsyncMock()
        repo.get_active_offers = AsyncMock(return_value=[satisfied, partial, bulk, dead])
        store = FakeStore(details={"name": "X", "price": 10.0})
        with patch("src.app.modules.offers.recommendations.OfferRepository",
                   return_value=repo):
            out = await get_recommendations_for_cart(
                "t1", cart, store_client=store, db_session_factory=lambda: FakeDB()
            )
        kinds = [s["kind"] for s in out]
        assert kinds[0] == "combo" and out[0]["satisfied"] is True
        assert kinds[1] == "combo" and out[1]["satisfied"] is False
        assert kinds[2] == "bulk"
        assert kinds[3] == "dead_stock"
        assert len(out) == 4

    @pytest.mark.asyncio
    async def test_orm_offers_handled_via_offer_to_dict(self):
        combo = _orm_offer(
            id="c1", offer_kind="combo", platform_id="10",
            combo_items=[{"platform_id": "10", "quantity": 2, "name": "Bag"}],
        )
        repo = AsyncMock()
        repo.get_active_offers = AsyncMock(return_value=[combo])
        with patch("src.app.modules.offers.recommendations.OfferRepository",
                   return_value=repo):
            out = await get_recommendations_for_cart(
                "t1", [_line(10, 1)], db_session_factory=lambda: FakeDB()
            )
        assert out[0]["offer_id"] == "c1"

    @pytest.mark.asyncio
    async def test_combo_enrichment_merges_store_data(self):
        combo = _offer(
            id="c1", offer_kind="combo", platform_id="10",
            combo_items=[
                {"platform_id": "10", "quantity": 1, "name": "Bag"},
                {"platform_id": "11", "quantity": 1, "name": "Shirt"},
            ],
        )
        repo = AsyncMock()
        repo.get_active_offers = AsyncMock(return_value=[combo])
        store = FakeStore(details={"name": "Live Bag", "price": 750.0})
        with patch("src.app.modules.offers.recommendations.OfferRepository",
                   return_value=repo):
            out = await get_recommendations_for_cart(
                "t1", [_line(10, 1), _line(11, 1)],
                store_client=store, db_session_factory=lambda: FakeDB()
            )
        assert len(out) == 1
        items = out[0]["combo_items"]
        assert items[0]["name"] == "Live Bag"
        assert items[0]["price"] == 750.0
        assert items[0]["quantity"] == 1

    @pytest.mark.asyncio
    async def test_bulk_enrichment_sets_name_and_unit_price(self):
        bulk = _offer(id="b1", offer_kind="bulk", platform_id="5",
                      bulk_tiers=[{"min_qty": 3, "discount_percent": 10}])
        repo = AsyncMock()
        repo.get_active_offers = AsyncMock(return_value=[bulk])
        store = FakeStore(details={"name": "Bulk Widget", "price": 120.0})
        with patch("src.app.modules.offers.recommendations.OfferRepository",
                   return_value=repo):
            out = await get_recommendations_for_cart(
                "t1", [_line(5, 1)], store_client=store,
                db_session_factory=lambda: FakeDB()
            )
        assert out[0]["name"] == "Bulk Widget"
        assert out[0]["unit_price"] == 120.0

    @pytest.mark.asyncio
    async def test_dead_stock_enrichment_sets_name_and_price(self):
        dead = _offer(id="d1", offer_kind="dead_stock", platform_id="6")
        repo = AsyncMock()
        repo.get_active_offers = AsyncMock(return_value=[dead])
        store = FakeStore(details={"name": "Old Stock", "price": 5.0})
        with patch("src.app.modules.offers.recommendations.OfferRepository",
                   return_value=repo):
            out = await get_recommendations_for_cart(
                "t1", [], store_client=store, db_session_factory=lambda: FakeDB()
            )
        assert out[0]["name"] == "Old Stock"
        assert out[0]["price"] == 5.0

    @pytest.mark.asyncio
    async def test_enrichment_without_store_client_is_empty_strings(self):
        combo = _offer(
            id="c1", offer_kind="combo", platform_id="10",
            combo_items=[{"platform_id": "10", "quantity": 1, "name": "Bag"}],
        )
        repo = AsyncMock()
        repo.get_active_offers = AsyncMock(return_value=[combo])
        with patch("src.app.modules.offers.recommendations.OfferRepository",
                   return_value=repo):
            out = await get_recommendations_for_cart(
                "t1", [_line(10, 1)], store_client=None,
                db_session_factory=lambda: FakeDB()
            )
        assert out[0]["combo_items"][0]["name"] == "Bag"  # falls back to offer name
        assert out[0]["combo_items"][0]["price"] == ""

    @pytest.mark.asyncio
    async def test_suggestion_limit_bounds_enrichment(self):
        lots = [_offer(id=f"o{i}", offer_kind="dead_stock", platform_id=str(i + 1))
                for i in range(10)]
        repo = AsyncMock()
        repo.get_active_offers = AsyncMock(return_value=lots)
        store = FakeStore(details={"name": "N", "price": 1.0})
        with patch("src.app.modules.offers.recommendations.OfferRepository",
                   return_value=repo):
            out = await get_recommendations_for_cart(
                "t1", [], store_client=store, db_session_factory=lambda: FakeDB(),
                limit=4
            )
        assert len(out) == 4

    @pytest.mark.asyncio
    async def test_repo_error_returns_empty_and_closes_db(self):
        repo = AsyncMock()
        repo.get_active_offers = AsyncMock(side_effect=RuntimeError("db down"))
        db = FakeDB()
        with patch("src.app.modules.offers.recommendations.OfferRepository",
                   return_value=repo):
            out = await get_recommendations_for_cart(
                "t1", [_line(1, 1)], db_session_factory=lambda: db
            )
        assert out == []
        assert db.closed is True