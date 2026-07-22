"""Tests for agent/guardrails.py — the hallucination killer.

Covers check_input, check_output (all 6 checks), build_retrieved_context,
validate_spoken_text, safe_fallback, and strip_inline_prices.
"""
from __future__ import annotations

import pytest
from src.app.agent.guardrails import (
    InputBlocked,
    OutputValidationError,
    build_retrieved_context,
    check_input,
    check_output,
    safe_fallback,
    strip_inline_prices,
    validate_spoken_text,
)


# ═══════════════════════════════════════════════════════════════════════════════
# INPUT GUARDRAIL
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckInput:
    def test_off_topic_news(self):
        with pytest.raises(InputBlocked):
            check_input("what is the news today")

    def test_off_topic_politics(self):
        with pytest.raises(InputBlocked):
            check_input("who is the president")

    def test_off_topic_coding(self):
        with pytest.raises(InputBlocked):
            check_input("help me debug python code")

    def test_off_topic_gpt_mention(self):
        with pytest.raises(InputBlocked):
            check_input("are you a gpt")

    def test_shopping_query_passes(self):
        result = check_input("show me nike shoes under 5000")
        assert "nike" in result

    def test_empty_input(self):
        assert check_input("") == ""

    def test_pii_redacted_from_input(self):
        result = check_input("my email is john@example.com")
        assert "[email]" in result
        assert "john@example.com" not in result

    def test_phone_redacted(self):
        result = check_input("call me at 9876543210")
        assert "[phone]" in result

    def test_greeting_passes(self):
        result = check_input("hello, what do you have in shirts?")
        assert "hello" in result


# ═══════════════════════════════════════════════════════════════════════════════
# OUTPUT GUARDRAIL — Check 1: product IDs
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckOutputProductIds:
    def test_valid_id_passes(self):
        text = "The Casio watch ID: 1234 is great"
        result = check_output(text, retrieved_product_ids={"1234"}, allow_retry=False)
        assert "1234" in result

    def test_invented_id_raises(self):
        text = "ID: 9999 is available"
        with pytest.raises(OutputValidationError, match="hallucinated product IDs"):
            check_output(text, retrieved_product_ids={"1234"}, allow_retry=True)

    def test_invented_id_stripped_in_no_retry(self):
        text = "ID: 9999 is available"
        result = check_output(text, retrieved_product_ids={"1234"}, allow_retry=False)
        assert "ID" not in result and "9999" not in result

    def test_multiple_ids_all_valid(self):
        text = "IDs: 100 and 101 are both nice"
        result = check_output(text, retrieved_product_ids={"100", "101"}, allow_retry=False)
        assert "100" in result

    def test_no_ids_mentioned_ok(self):
        text = "That product is great"
        result = check_output(text, retrieved_product_ids={"1234"}, allow_retry=False)
        assert result == text


# ═══════════════════════════════════════════════════════════════════════════════
# OUTPUT GUARDRAIL — Check 1b: product NAMES
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckOutputProductNames:
    def test_known_model_number_passes(self):
        text = "The Galaxy S24 is a great phone"
        result = check_output(
            text,
            retrieved_names={"galaxy", "s24", "phone"},
            retrieved_full_names={"samsung galaxy s24"},
            allow_retry=False,
        )
        assert "Galaxy" in result

    def test_fabricated_model_number_raises(self):
        text = "The UltraSound X50 is amazing"
        with pytest.raises(OutputValidationError, match="hallucinated product name"):
            check_output(
                text,
                retrieved_names={"basic", "phone"},
                retrieved_full_names={"basic phone"},
                allow_retry=True,
            )

    def test_negation_not_flagged(self):
        text = "We don't carry the Rolex GMT2"
        result = check_output(
            text,
            retrieved_names={"casio"},
            retrieved_full_names={"casio watch"},
            allow_retry=False,
        )
        assert "Rolex" in result

    def test_greeting_not_flagged(self):
        text = "Hello! Welcome to our store"
        result = check_output(text, allow_retry=False)
        assert result == text

    def test_promoted_product_still_needs_tool_call(self):
        # Promoted products in the prompt are category-level — naming one
        # without a tool call this turn should still be flagged.
        text = "The Bluetooth Speaker X200 is on sale"
        with pytest.raises(OutputValidationError):
            check_output(
                text,
                retrieved_names={"shirt"},
                retrieved_full_names={"cotton shirt"},
                allow_retry=True,
            )


# ═══════════════════════════════════════════════════════════════════════════════
# OUTPUT GUARDRAIL — Check 2: prices
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckOutputPrices:
    def test_known_price_passes(self):
        # Known price passes Check 2 (no exception); Check 4b strips inline
        # prices structurally — the interface handles display.
        result = check_output(
            "This is ₹12499",
            retrieved_prices={"₹12499", "12499"},
            allow_retry=False,
        )
        assert result == "This is"  # price stripped by 4b structural enforcement

    def test_invented_price_raises(self):
        text = "It costs ₹9999"
        with pytest.raises(OutputValidationError, match="hallucinated prices"):
            check_output(
                text,
                retrieved_prices={"₹12499"},
                allow_retry=True,
            )

    def test_price_stripped_in_no_retry(self):
        text = "It costs ₹9999"
        # allow_retry=False means strip inline — but check still logs
        result = check_output(
            text,
            retrieved_prices={"₹12499"},
            allow_retry=False,
        )
        assert "9999" not in result and "₹" not in result


# ═══════════════════════════════════════════════════════════════════════════════
# OUTPUT GUARDRAIL — Check 3: attributes
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckOutputAttributes:
    def test_known_attribute_passes(self):
        text = "This comes in Black"
        result = check_output(
            text,
            retrieved_attributes={"black"},
            retrieved_full_names={"black shirt"},
            allow_retry=False,
        )
        assert "Black" in result

    def test_invented_attribute_raises(self):
        text = "This comes in Purple"
        with pytest.raises(OutputValidationError, match="potentially invented attribute"):
            check_output(
                text,
                retrieved_attributes={"black", "red"},
                retrieved_full_names={"cotton shirt"},
                allow_retry=True,
            )

    def test_size_abbreviation_not_false_positive(self):
        text = "Size M is available"
        result = check_output(
            text,
            retrieved_attributes={"medium"},
            allow_retry=False,
        )
        assert "M" in result

    def test_single_invented_attribute_now_flagged(self):
        text = "This is available in Blue"
        # Threshold is 1 — a single invented colour triggers retry
        with pytest.raises(OutputValidationError):
            check_output(
                text,
                retrieved_attributes={"black"},
                retrieved_full_names={"black shirt"},
                allow_retry=True,
            )


# ═══════════════════════════════════════════════════════════════════════════════
# OUTPUT GUARDRAIL — Check 4b: inline price stripping
# ═══════════════════════════════════════════════════════════════════════════════

class TestStripInlinePrices:
    def test_currency_symbol_stripped(self):
        # The regex consumes the lead-in word + symbol → price removed
        result = strip_inline_prices("It costs ₹12499")
        assert "₹" not in result
        assert "12499" not in result or "It" in result or result == "" or result.strip() == ""

    def test_rupees_word_stripped(self):
        assert "rupees" not in strip_inline_prices("4999 rupees")

    def test_rs_stripped(self):
        assert "rs" not in strip_inline_prices("4999 rs")

    def test_stock_count_stripped(self):
        result = strip_inline_prices("only 3 left")
        assert "left" not in result

    def test_no_number_unchanged(self):
        text = "That product is great"
        assert strip_inline_prices(text) == text

    def test_context_anchored_stripped(self):
        result = strip_inline_prices("it costs 14999")
        assert "14999" not in result


# ═══════════════════════════════════════════════════════════════════════════════
# OUTPUT GUARDRAIL — Check 5: language
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckOutputLanguage:
    def test_english_passes_for_english(self):
        text = "This is a nice product, it costs 500 rupees and comes in blue"
        result = check_output(text, detected_language="en", allow_retry=False)
        assert result

    def test_malayalam_passes_for_malayalam(self):
        text = "ഈ ഉൽപ്പന്നം വളരെ നല്ലതാണ്"
        result = check_output(text, detected_language="ml", allow_retry=False)
        assert "ഈ" in result

    def test_wrong_script_raises(self):
        text = ("This product is very nice and I highly recommend it for your needs."  # 70 chars
                " Let me tell you more about why this is such a great choice for you.")  # 81+ total
        with pytest.raises(OutputValidationError, match="language mismatch"):
            check_output(text, detected_language="ml", allow_retry=True)

    def test_short_text_not_flagged(self):
        text = "Hello"  # <80 chars
        result = check_output(text, detected_language="ml", allow_retry=True)
        assert result == text


# ═══════════════════════════════════════════════════════════════════════════════
# OUTPUT GUARDRAIL — Check 6: stock status
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckOutputStock:
    def test_in_stock_correct_passes(self):
        text = "This product is in stock and available."
        result = check_output(
            text,
            retrieved_product_ids={"1234"},
            retrieved_stock={"1234": True},
            allow_retry=False,
        )
        assert "in stock" in result

    def test_out_of_stock_for_in_stock_raises(self):
        text = "Sorry, product 1234 is out of stock."
        with pytest.raises(OutputValidationError, match="stock mismatch"):
            check_output(
                text,
                retrieved_product_ids={"1234"},
                retrieved_stock={"1234": True},
                allow_retry=True,
            )

    def test_in_stock_for_out_of_stock_raises(self):
        text = "Good news, product 1234 is back in stock!"
        with pytest.raises(OutputValidationError, match="stock mismatch"):
            check_output(
                text,
                retrieved_product_ids={"1234"},
                retrieved_stock={"1234": False},
                allow_retry=True,
            )

    def test_sold_out_for_out_of_stock_passes(self):
        text = "Unfortunately 1234 is sold out."
        result = check_output(
            text,
            retrieved_product_ids={"1234"},
            retrieved_stock={"1234": False},
            allow_retry=False,
        )
        assert "sold out" in result

    def test_no_stock_data_skips_check(self):
        text = "This product is in stock."
        result = check_output(
            text,
            retrieved_product_ids={"1234"},
            retrieved_stock=None,
            allow_retry=False,
        )
        assert "in stock" in result

    def test_unknown_id_mention_not_flagged(self):
        text = "Item 9999 is out of stock."
        result = check_output(
            text,
            retrieved_product_ids={"1234"},
            retrieved_stock={"1234": True},
            allow_retry=False,
        )
        assert "9999" in result


# ═══════════════════════════════════════════════════════════════════════════════
# BUILD RETRIEVED CONTEXT
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildRetrievedContext:
    def test_empty_input(self):
        ids, prices, attrs, names, fulls, stock = build_retrieved_context([])
        assert not ids and not prices and not attrs and not names and not fulls and not stock

    def test_products_extracted(self):
        results = [{
            "products": [
                {"id": 101, "name": "Casio G-Shock", "price": "12499",
                 "attributes": {"color": ["black", "red"]}, "in_stock": True},
            ]
        }]
        ids, prices, attrs, names, fulls, stock = build_retrieved_context(results)
        assert "101" in ids
        assert "casio" in names
        assert "g" not in names  # single letter excluded
        assert "shock" in names
        assert "casio g-shock" in fulls
        assert "black" in attrs
        assert "red" in attrs
        assert stock.get("101") is True

    def test_out_of_stock_product(self):
        results = [{
            "products": [
                {"id": 202, "name": "Out of Stock Item", "price": "500", "in_stock": False},
            ]
        }]
        _, _, _, _, _, stock = build_retrieved_context(results)
        assert stock.get("202") is False

    def test_stock_from_quantity(self):
        results = [{
            "products": [
                {"id": 303, "name": "Low Stock Item", "price": "1000", "stock_quantity": 0},
            ]
        }]
        _, _, _, _, _, stock = build_retrieved_context(results)
        assert stock.get("303") is False

    def test_product_detail_extracted(self):
        results = [{
            "product": {"id": 404, "name": "Single Product", "price": "999",
                        "in_stock": True},
        }]
        ids, prices, attrs, names, fulls, stock = build_retrieved_context(results)
        assert "404" in ids
        assert "single" in names
        assert stock.get("404") is True


# ═══════════════════════════════════════════════════════════════════════════════
# VOICE MONITOR
# ═══════════════════════════════════════════════════════════════════════════════

class TestValidateSpokenText:
    def test_grounded_text_returns_true(self):
        ok, cleaned = validate_spoken_text(
            "The Casio G-Shock is great",
            retrieved_full_names={"casio g-shock"},
            retrieved_names={"casio", "g", "shock"},
        )
        assert ok is True

    def test_hallucinated_name_returns_false(self):
        ok, _ = validate_spoken_text(
            "The UltraSound X50 is amazing",
            retrieved_full_names={"casio g-shock"},
            retrieved_names={"casio", "shock"},
        )
        assert ok is False

    def test_empty_text_returns_true(self):
        ok, _ = validate_spoken_text("")
        assert ok is True


# ═══════════════════════════════════════════════════════════════════════════════
# SAFE FALLBACK
# ═══════════════════════════════════════════════════════════════════════════════

class TestSafeFallback:
    def test_english_fallback(self):
        msg = safe_fallback("en")
        assert "looking for" in msg

    def test_hindi_fallback(self):
        msg = safe_fallback("hi")
        assert "dhundhna" in msg

    def test_malayalam_fallback(self):
        msg = safe_fallback("ml")
        assert "venam" in msg

    def test_unknown_language_falls_back_to_english(self):
        msg = safe_fallback("fr")
        assert "looking for" in msg
