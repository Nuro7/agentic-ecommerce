"""Unit tests for order-webhook status mapping + timestamp parsing.

These cover the pure logic that decides whether a captured Shopify order counts
toward revenue (status == "completed") — the part analytics depends on.
"""
from datetime import timezone

from src.app.modules.webhooks.service import _map_order_status, _parse_order_ts


def test_paid_maps_to_completed():
    assert _map_order_status({"financial_status": "paid"}) == "completed"


def test_partially_refunded_counts_as_completed():
    assert _map_order_status({"financial_status": "partially_refunded"}) == "completed"


def test_pending_and_missing_stay_pending():
    assert _map_order_status({"financial_status": "pending"}) == "pending"
    assert _map_order_status({"financial_status": "authorized"}) == "pending"
    assert _map_order_status({}) == "pending"


def test_refunded_and_voided_are_cancelled():
    assert _map_order_status({"financial_status": "refunded"}) == "cancelled"
    assert _map_order_status({"financial_status": "voided"}) == "cancelled"


def test_cancelled_at_overrides_financial_status():
    assert _map_order_status(
        {"financial_status": "paid", "cancelled_at": "2026-07-15T00:00:00Z"}
    ) == "cancelled"


def test_parse_ts_handles_z_suffix():
    dt = _parse_order_ts("2026-07-15T10:30:00Z")
    assert (dt.year, dt.month, dt.day) == (2026, 7, 15)
    assert dt.tzinfo is not None


def test_parse_ts_handles_offset():
    dt = _parse_order_ts("2026-07-15T10:30:00-04:00")
    assert dt.tzinfo is not None


def test_parse_ts_bad_value_falls_back_to_now_utc():
    assert _parse_order_ts("not-a-date").tzinfo == timezone.utc


def test_parse_ts_none_falls_back_to_now_utc():
    assert _parse_order_ts(None).tzinfo == timezone.utc
