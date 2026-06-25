"""Language-detection hardening (stop the false `ml` latch).

- detect_language: a non-Latin script wins only with enough chars + ratio, so one
  stray glyph in an English message stays English, but a genuine short message detects.
- _resolve_language: non-English locks immediately, but English decays the lock back
  to 'en' over 3 consecutive clean-English turns (no instant flip-flop, no permanent stick).
"""
from src.app.agent.prompts.filtering import detect_language
from src.app.agent.brain.core import _resolve_language, _ENGLISH_RESET_STREAK


# ── detect_language: ratio + min-count ────────────────────────────────────────

def test_pure_english_is_en():
    assert detect_language("show me red running shoes under 2000") == "en"


def test_one_stray_malayalam_glyph_stays_english():
    # A single Malayalam char in an English sentence (noisy STT) must NOT flip.
    assert detect_language("show me shirts ഷ") == "en"


def test_short_genuine_malayalam_detects_ml():
    assert detect_language("നന്ദി") == "ml"


def test_malayalam_sentence_detects_ml():
    assert detect_language("എനിക്ക് ഒരു ഷൂ വേണം") == "ml"


def test_hinglish_still_detects_hi():
    assert detect_language("bhai mujhe kya milega") == "hi"


def test_romanized_malayalam_still_detects_ml():
    assert detect_language("njan veno ente") == "ml"


def test_empty_is_en():
    assert detect_language("") == "en"


# ── _resolve_language: lock + English-decay ───────────────────────────────────

def test_non_english_locks_immediately_and_resets_streak():
    assert _resolve_language("ml", "en", 2) == ("ml", 0)


def test_single_english_turn_keeps_sticky_language():
    # One English turn after ml: stays ml, streak ticks up.
    assert _resolve_language("en", "ml", 0) == ("ml", 1)


def test_english_decays_to_en_after_threshold_turns():
    lang, streak = "ml", 0
    # Simulate consecutive English turns.
    for _ in range(_ENGLISH_RESET_STREAK - 1):
        lang, streak = _resolve_language("en", lang, streak)
        assert lang == "ml"  # still locked before the threshold
    lang, streak = _resolve_language("en", lang, streak)
    assert lang == "en"  # decayed back to English on the threshold turn


def test_english_stays_english():
    assert _resolve_language("en", "en", 0) == ("en", 1)
