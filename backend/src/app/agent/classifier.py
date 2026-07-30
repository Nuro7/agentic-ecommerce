"""
Intent Classifier — regex-only (~0ms, no API calls).

Intent taxonomy (9 classes):
  search         → product discovery, browse, "show me X", "find X"
  cart_action    → add / remove / update / view cart
  chitchat       → greetings, thanks, "ok", "yes", "no", small talk
  order_status   → track order, order history, "where is my order"
  checkout       → place order, address collection, payment confirm
  store_info     → shipping / returns / payment methods / store about
  product_detail → more info about a product, compare products
  inventory      → stock check, size/color availability
  off_topic      → not shopping-related (guardrail)

Routing contract (used by orchestrator):
  chitchat       → cached canned response (no LLM, <1ms)
  off_topic      → guardrail rejection (no LLM, <1ms)
  store_info     → fast deterministic handler (no LLM, env vars)
  cart_action    → fast deterministic handler OR LLM for complex ops
  search, product_detail, inventory, checkout, order_status → LLM agent
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── Intent labels ─────────────────────────────────────────────────────────────

SEARCH         = "search"
CART_ACTION    = "cart_action"
CHITCHAT       = "chitchat"
ORDER_STATUS   = "order_status"
CHECKOUT       = "checkout"
STORE_INFO     = "store_info"
PRODUCT_DETAIL = "product_detail"
INVENTORY      = "inventory"
OFF_TOPIC      = "off_topic"


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class IntentResult:
    intent:      str
    confidence:  float          # 0.0–1.0
    query:       str   = ""     # normalised search term (intent == search)
    product_ref: str   = ""     # product name/ID mentioned in message
    quantity:    int   = 1      # quantity for cart ops
    via:         str   = "regex"  # "regex" | "fallback"
    latency_ms:  float = 0.0

    # ── Routing helpers ───────────────────────────────────────────────────────

    @property
    def is_shopping(self) -> bool:
        """False only for off_topic — all shopping intents return True."""
        return self.intent != OFF_TOPIC

    @property
    def needs_llm(self) -> bool:
        """True when the full LLM agent must handle this intent."""
        return self.intent in {SEARCH, PRODUCT_DETAIL, INVENTORY, CHECKOUT, ORDER_STATUS}

    @property
    def is_fast_path(self) -> bool:
        """True when a cached / deterministic handler can answer without LLM."""
        return self.intent in {STORE_INFO, CHITCHAT, OFF_TOPIC}

    def __repr__(self) -> str:
        return (
            f"IntentResult(intent={self.intent!r}, conf={self.confidence:.2f}, "
            f"via={self.via!r}, q={self.query!r})"
        )



# ── Regex classifier (fast, no API calls) ────────────────────────────────────

class _RegexClassifier:
    """
    Fast (0ms) regex-based classifier.
    """

    # ── Pattern sets ──────────────────────────────────────────────────────────

    _CHITCHAT = re.compile(
        r"\b(hi|hello|hey|hiya|namaste|vanakkam|namaskar|ayubowan|salam|"
        r"thanks|thank you|shukriya|dhanyavaad|nandri|thanks a lot|"
        r"ok|okay|alright|sure|got it|understood|noted|cool|great|nice|"
        r"bye|goodbye|see you|take care|"
        # single word affirmatives
        r"yes|no|nope|yep|yeah|nahi|haan|seri|ayyo|aayi|avunu)\b",
        re.I,
    )

    _STORE_INFO = re.compile(
        r"\b(shipping|delivery charge|delivery fee|free delivery|free shipping|"
        r"returns?|refund|exchange|return policy|"
        r"payment|pay|cod|cash on delivery|upi|credit card|debit card|net banking|"
        r"store info|about (the )?store|what is this store|who are you|"
        r"store name|store hours|contact|support)\b",
        re.I,
    )

    _CART_ACTION = re.compile(
        r"\b(add to cart|add (it|this|that)|remove (from cart|item|this|that)|"
        r"delete from cart|update cart|change quantity|my cart|show cart|"
        r"view cart|cart total|what('s| is) in my cart|cart mein|"
        r"cart il|cart add|remove karo|add karo|cart dekho)\b",
        re.I,
    )

    _ORDER_STATUS = re.compile(
        r"\b(my orders?|order status|track (my )?order|where is my order|"
        r"order history|past orders?|previous orders?|order id|order number|"
        r"mera order|order kahan|order track)\b",
        re.I,
    )

    _CHECKOUT = re.compile(
        r"\b(checkout|place (an? )?order|buy now|purchase|confirm order|"
        r"proceed to pay|pay now|address|pincode|delivery address|"
        r"order (kar|karo|cheyyuka|seyyungal)|checkout cheyyuka)\b",
        re.I,
    )

    _INVENTORY = re.compile(
        r"\b(available|in stock|out of stock|stock|size (available|check)|"
        r"do you have (in|size)|is (it|this|that) available|"
        r"color (available|options)|which (sizes?|colors?) (are )?available|"
        r"undoo|size undu|available aanu)\b",
        re.I,
    )

    _PRODUCT_DETAIL = re.compile(
        r"\b(tell me more|more (details?|info(rmation)?)|what (is|are) (the )?features?|"
        r"describe|specifications?|specs?|compare|versus|vs\.?|difference between|"
        r"which (is|one) better|price of|how much (is|does)|cost of)\b",
        re.I,
    )

    _OFF_TOPIC = re.compile(
        r"\b(weather|news|sports?|cricket|football|politics?|movie|film|"
        r"recipe|cook|medical|doctor|medicine|hospital|"
        r"write (a |an )?(code|program|essay|story)|"
        r"translate|explain (quantum|physics|chemistry)|"
        r"capital of|president of|prime minister)\b",
        re.I,
    )

    def classify(self, message: str) -> IntentResult:
        t0 = time.monotonic()
        text = (message or "").strip()

        # Priority order matters — more specific patterns checked first
        if self._OFF_TOPIC.search(text):
            intent, conf = OFF_TOPIC, 0.85
        elif self._CHITCHAT.search(text) and len(text.split()) <= 5:
            intent, conf = CHITCHAT, 0.90
        elif self._ORDER_STATUS.search(text):
            intent, conf = ORDER_STATUS, 0.88
        elif self._CHECKOUT.search(text):
            intent, conf = CHECKOUT, 0.85
        elif self._CART_ACTION.search(text):
            intent, conf = CART_ACTION, 0.85
        elif self._STORE_INFO.search(text):
            intent, conf = STORE_INFO, 0.88
        elif self._INVENTORY.search(text):
            intent, conf = INVENTORY, 0.80
        elif self._PRODUCT_DETAIL.search(text):
            intent, conf = PRODUCT_DETAIL, 0.78
        else:
            # Default: treat as product search
            intent, conf = SEARCH, 0.60

        return IntentResult(
            intent=intent,
            confidence=conf,
            query=text if intent == SEARCH else "",
            via="regex",
            latency_ms=round((time.monotonic() - t0) * 1000, 2),
        )


# ── Main classifier ───────────────────────────────────────────────────────────

class IntentClassifier:
    """
    Regex-only intent classifier (~0ms, no API calls).
    Thread-safe singleton — call get_classifier() instead of instantiating directly.
    """

    def __init__(self) -> None:
        self._regex = _RegexClassifier()

    # ── Public API ────────────────────────────────────────────────────────────

    async def classify(
        self,
        message: str,
        language: str = "en",
    ) -> IntentResult:
        if not message or not message.strip():
            return IntentResult(intent=CHITCHAT, confidence=1.0, via="fallback")

        result = self._regex.classify(message)
        logger.debug("Classifier regex: %s", result)
        return result


# ── Singleton ─────────────────────────────────────────────────────────────────

_classifier_instance: Optional[IntentClassifier] = None


def get_classifier() -> IntentClassifier:
    global _classifier_instance
    if _classifier_instance is None:
        _classifier_instance = IntentClassifier()
    return _classifier_instance
