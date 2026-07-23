"""Tests for canned.py say() function — the multi-language template formatter."""
import pytest

from src.app.agent.brain.canned import say


# ── Normal cases ──────────────────────────────────────────────────────────────

def test_say_basic():
    result = say("en", "cart_empty")
    assert result == "Your cart is empty right now."


def test_say_with_simple_kwargs():
    result = say("en", "removed_from_cart", name="T-Shirt")
    assert "T-Shirt" in result


def test_say_with_qty():
    result = say("en", "added_to_cart", name="Sneakers", qty=2)
    assert "Sneakers" in result
    assert "2" in result or "qty" in result


def test_say_with_store_name():
    result = say("en", "store_info", store_name="MyStore")
    assert "MyStore" in result


def test_say_unknown_key_returns_empty():
    result = say("en", "nonexistent_key")
    assert result == ""


# ── Edge cases: values with curly braces ──────────────────────────────────────

def test_say_with_json_string_value():
    """Should not crash when a kwarg value contains JSON-like curly braces."""
    result = say(
        "en", "added_to_cart",
        name='{"Size": "M", "Color": "Black"}',
        qty=1,
    )
    assert "{" in result


def test_say_with_dict_string_value():
    """Should not crash when a kwarg value contains a Python dict string."""
    result = say(
        "en", "out_of_stock",
        name="Product",
        size="{'Size': 'M', 'Color': 'Black'}",
    )
    assert "Product" in result


def test_say_product_name_with_braces():
    """Product names from feeds may contain braces — should not crash."""
    result = say("en", "products_found", name="Shirt {Size: M} (Blue)")
    assert "Shirt" in result


def test_say_availability_with_dict_attrs():
    """Availability template with attribute-like kwargs should not crash."""
    result = say(
        "en", "availability",
        name="Shoe",
        size='{"Size": "M", "Color": "Black"}',
        in_stock=True,
        qty=5,
    )
    assert "Shoe" in result


# ── Language fallback ─────────────────────────────────────────────────────────

def test_say_unsupported_lang_falls_back_to_en():
    result = say("fr", "cart_empty")
    assert result == "Your cart is empty right now."


def test_say_hindi():
    result = say("hi", "cart_empty")
    assert result == "Aapka cart abhi khaali hai."


def test_say_kannada_with_kwargs():
    result = say("kn", "added_to_cart", name="ಪುಸ್ತಕ", qty=1)
    assert "ಪುಸ್ತಕ" in result
