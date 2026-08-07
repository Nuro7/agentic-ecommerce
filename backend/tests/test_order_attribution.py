"""Tests for order attribution: recovering the Speako session marker from the
webhook payload + parsing line items into order_items rows."""
from src.app.modules.webhooks.service import _order_attribution, _order_line_items


def test_attribution_reads_note_attributes():
    payload = {
        "note_attributes": [
            {"name": "other", "value": "x"},
            {"name": "_speako_session", "value": "sess_abc123"},
        ]
    }
    assert _order_attribution(payload) == {
        "session_id": "sess_abc123",
        "source": "agent",
    }


def test_attribution_none_without_marker():
    assert _order_attribution({"note_attributes": [{"name": "anything", "value": "1"}]}) == {
        "session_id": None,
        "source": None,
    }


def test_attribution_flat_note_fallback():
    payload = {"note": "Order note _speako_session=xyz-999 thanks"}
    result = _order_attribution(payload)
    assert result["session_id"] == "xyz-999"
    assert result["source"] == "agent"


def test_line_items_parse():
    items = _order_line_items({
        "line_items": [
            {"product_id": 10, "title": "Widget", "quantity": 2, "price": "9.99"},
            {"product_id": 20, "title": "Gadget", "quantity": 1, "price": "4.50", "sku": "G-1"},
        ]
    })
    assert len(items) == 2
    w = items[0]
    assert w["product_id"] == "10"
    assert w["name"] == "Widget"
    assert w["quantity"] == 2
    assert w["total"] == 19.98  # price * qty
    assert items[1]["sku"] == "G-1"


def test_line_items_missing_price_still_has_revenue():
    items = _order_line_items({"line_items": [{"product_id": 3, "title": "Freebie", "quantity": 1}]})
    assert items[0]["total"] == 0.0