"""P1-8/8b/8c/9 — anti-hallucination guardrail unit tests.

Pure unit: call check_output() directly with constructed grounding sets (no DB/Redis).
retrieved_names = token set (model-number literal check, P1-8b);
retrieved_full_names = whole product names (digit-free fuzzy match, P1-8) — kept separate.
"""
import pytest

from src.app.agent.guardrails import check_output, OutputValidationError, validate_spoken_text


# ── P1-8: digit-free fabricated names ────────────────────────────────────────

def test_digit_free_fake_name_rejected():
    with pytest.raises(OutputValidationError):
        check_output(
            "We have the Malabar Special Masala in stock.",
            retrieved_names={"organic", "turmeric", "powder"},
            retrieved_full_names={"organic turmeric powder"},
        )


def test_real_digit_free_name_passes():
    out = check_output(
        "I recommend the Royal Mysore Silk Saree.",
        retrieved_names={"royal", "mysore", "silk", "saree"},
        retrieved_full_names={"royal mysore silk saree"},
    )
    assert isinstance(out, str)


def test_negated_fake_name_passes():
    # "we don't carry …" is the agent saying it does NOT have it — never a hallucination.
    out = check_output(
        "Sorry, we don't carry Malabar Masala right now.",
        retrieved_names={"organic", "turmeric", "powder"},
        retrieved_full_names={"organic turmeric powder"},
    )
    assert isinstance(out, str)


# ── Correction 3: real-catalog reorder/partial MUST pass; near-miss MUST fail ─

def test_real_name_reordered_tokens_passes():
    # Customer/agent says the catalog name with tokens reordered — must still ground.
    out = check_output(
        "Try our Garam Masala Malabar blend.",
        retrieved_names={"malabar", "garam", "masala"},
        retrieved_full_names={"malabar special garam masala"},
    )
    assert isinstance(out, str)


def test_real_name_partial_tokens_passes():
    # Partial mention (drops the leading "Royal") of a real product — must pass at 0.80.
    out = check_output(
        "The Mysore Silk Saree is a great pick.",
        retrieved_names={"royal", "mysore", "silk", "saree"},
        retrieved_full_names={"royal mysore silk saree"},
    )
    assert isinstance(out, str)


def test_near_miss_fake_name_rejected():
    # One distinctive token swapped (Kanchipuram≠Mysore) — a different real-world saree
    # NOT in this catalog. Must fail so Aria can't pass off a product it doesn't have.
    with pytest.raises(OutputValidationError):
        check_output(
            "We have the Kanchipuram Silk Saree available.",
            retrieved_names={"royal", "mysore", "silk", "saree"},
            retrieved_full_names={"royal mysore silk saree"},
        )


# ── P1-8b: numbered-variant fakes (literal model-token match vs retrieved_names) ─

def test_numbered_variant_fake_rejected():
    # Only S24 was retrieved; "Galaxy S25" shares "galaxy" but the model token s25 isn't real.
    with pytest.raises(OutputValidationError):
        check_output(
            "The Galaxy S25 is the latest model.",
            retrieved_names={"galaxy", "s24", "samsung"},
            retrieved_full_names={"samsung galaxy s24"},
        )


def test_real_numbered_variant_passes():
    out = check_output(
        "The Galaxy S24 is in stock.",
        retrieved_names={"galaxy", "s24", "samsung"},
        retrieved_full_names={"samsung galaxy s24"},
    )
    assert isinstance(out, str)


# ── P1-8c: symbol-less / currency-word prices ────────────────────────────────

def test_symbolless_fake_price_rejected():
    with pytest.raises(OutputValidationError):
        check_output(
            "It costs 9999 rupees.",
            retrieved_prices={"2499", "₹2499"},
        )


def test_symbolless_real_price_passes():
    out = check_output(
        "It costs 2499 rupees.",
        retrieved_prices={"2499", "₹2499"},
    )
    assert isinstance(out, str)


# ── P1-11: voice transcript monitor (validate_spoken_text, never raises) ──────

def test_spoken_text_flags_fabricated_name():
    # Gemini spoke a saree the catalog doesn't have → monitor returns is_grounded=False.
    ok, _ = validate_spoken_text(
        "Sure, we have the Kanchipuram Silk Saree for you.",
        retrieved_names={"royal", "mysore", "silk", "saree"},
        retrieved_full_names={"royal mysore silk saree"},
    )
    assert ok is False


def test_spoken_text_passes_grounded():
    ok, _ = validate_spoken_text(
        "The Royal Mysore Silk Saree is a lovely choice.",
        retrieved_names={"royal", "mysore", "silk", "saree"},
        retrieved_full_names={"royal mysore silk saree"},
    )
    assert ok is True


# ── Fix 3: empty retrieval must still block fabricated product NAME claims ─────
# When a search returns zero products, both grounding sets are empty. Naming a
# model-numbered product anyway is the hallucination — it must NOT pass just
# because there's nothing to compare against. Greetings/negations stay safe.

def test_empty_retrieval_named_product_rejected():
    # Zero products retrieved → empty sets. "Audemars Piguet X1007" carries a model
    # token with no grounding → must fail (this is the production bug being closed).
    with pytest.raises(OutputValidationError):
        check_output(
            "We have the Audemars Piguet X1007 for ₹12,500.",
            retrieved_names=set(),
            retrieved_full_names=set(),
        )


def test_empty_retrieval_greeting_passes():
    # False-positive guard: a benign greeting with empty retrieval must NOT raise.
    out = check_output(
        "Hi! Welcome to the store. How can I help you today?",
        retrieved_names=set(),
        retrieved_full_names=set(),
    )
    assert isinstance(out, str)


def test_empty_retrieval_negation_passes():
    # False-positive guard: "we don't carry X" is the agent saying it does NOT have
    # the item — never a hallucination — even with a model token and empty sets.
    out = check_output(
        "Sorry, we don't carry the Rolex GMT2 right now.",
        retrieved_names=set(),
        retrieved_full_names=set(),
    )
    assert isinstance(out, str)
