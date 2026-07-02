"""
Intent Classifier — xAI Grok primary (~50ms) + regex fallback (0ms)

PRE-PROCESSING step in the Brain pipeline.
Runs in parallel with session loading so it adds ZERO latency to the
overall response time when the session load is the bottleneck.

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

Configuration:
  GROK_CLASSIFIER_MODEL  xAI Grok model for intent classification
                         Default: grok-3-mini-fast  (fast, cheap, ~50ms)
  GROK_API_KEY           Required to enable xAI Grok path (xai-...)
"""
from __future__ import annotations

import json
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

ALL_INTENTS = frozenset({
    SEARCH, CART_ACTION, CHITCHAT, ORDER_STATUS,
    CHECKOUT, STORE_INFO, PRODUCT_DETAIL, INVENTORY, OFF_TOPIC,
})


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class IntentResult:
    intent:      str
    confidence:  float          # 0.0–1.0
    query:       str   = ""     # normalised search term (intent == search)
    product_ref: str   = ""     # product name/ID mentioned in message
    quantity:    int   = 1      # quantity for cart ops
    via:         str   = "regex"  # "groq" | "regex" | "fallback"
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


# ── Groq system prompt (kept compact for minimum token cost) ─────────────────

_SYSTEM_PROMPT = """\
You are an intent classifier for a multilingual e-commerce shopping assistant.

Classify the user message into EXACTLY ONE of these intents:

search         – looking for products, browsing, "show me X", "find X", "what do you have"
cart_action    – add/remove/update/view cart items
chitchat       – greetings (hi/hello/namaste), thanks, small talk, "ok", "yes", "no", affirmations
order_status   – track order, order history, "where is my order", "my orders"
checkout       – place order, confirm purchase, pay now, address, COD, UPI
store_info     – shipping policy, return policy, payment methods, about the store, delivery charges
product_detail – details about a specific product, compare products, "tell me more", price query
inventory      – check stock, size availability, color options, "do you have in M", "is it available"
off_topic      – not related to shopping at all (news, sports, coding, medical advice, etc.)

Rules:
- If the message is in Hindi/Malayalam/Tamil/Telugu/Bengali/Kannada still classify correctly.
- "yes", "ok", "sure", "alright", single word replies to a conversation → chitchat
- A message that mentions a product name AND "add" / "buy" / "cart" → cart_action
- A message that mentions a product name WITHOUT cart/buy/add → search or product_detail
- Greetings like "hi", "hello", "namaste", "ayubowan", "vanakkam" → chitchat
- Shopping for products the store may sell (spices, medicines, cookware, books, …) → search,
  even when the message mentions cooking/health context ("masala to cook biryani" → search).
  Only requests for ADVICE itself (diagnosis, dosage, a recipe, a poem) → off_topic.

Respond with ONLY valid JSON (no markdown, no explanation):
{"intent": "...", "confidence": 0.95, "query": "search term if search intent else empty", "product_ref": "product name if mentioned else empty"}
"""


# ── Regex fallback patterns ───────────────────────────────────────────────────
# These mirror the original orchestrator._has_*_intent() methods but are
# consolidated here as a single fallback when Groq is unavailable.

class _RegexClassifier:
    """
    Fast (0ms) regex-based classifier.
    Used as fallback when Groq is unavailable or times out.
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
    Two-tier intent classifier:
      Tier 1 — xAI Grok grok-3-mini-fast  (~50ms, structured JSON output)
      Tier 2 — Regex patterns              (~0ms, fallback)

    Thread-safe singleton — call get_classifier() instead of instantiating directly.
    """

    def __init__(self) -> None:
        import os
        self._model = os.environ.get("GROK_CLASSIFIER_MODEL", "grok-3-mini-fast")
        self._timeout = float(os.environ.get("CLASSIFIER_TIMEOUT_S", "8.0"))
        self._regex = _RegexClassifier()
        self._groq = None   # variable name kept for internal compat
        self._init_grok()

    def _init_grok(self) -> None:
        import os
        key = os.environ.get("GROK_API_KEY", "").strip()
        if not key:
            logger.info("IntentClassifier: GROK_API_KEY not set — regex fallback only")
            return
        try:
            from openai import AsyncOpenAI
            self._groq = AsyncOpenAI(
                api_key=key,
                base_url="https://api.x.ai/v1",
                max_retries=0,
                timeout=self._timeout,
            )
            logger.info(
                "IntentClassifier: xAI Grok (%s) ready — timeout=%.1fs",
                self._model, self._timeout,
            )
        except Exception as exc:
            logger.warning("IntentClassifier: xAI Grok init failed: %s", exc)

    # ── Public API ────────────────────────────────────────────────────────────

    async def classify(
        self,
        message: str,
        language: str = "en",
    ) -> IntentResult:
        """
        Classify a user message.

        Tries Groq first; falls back to regex on any failure.
        Never raises — always returns a valid IntentResult.
        """
        if not message or not message.strip():
            return IntentResult(intent=CHITCHAT, confidence=1.0, via="fallback")

        if self._groq is not None:
            try:
                result = await self._classify_via_grok(message, language)
                logger.debug("Classifier Grok (xAI): %s", result)
                return result
            except Exception as exc:
                logger.warning(
                    "IntentClassifier xAI Grok failed (%s: %s) — using regex",
                    type(exc).__name__, exc,
                )

        result = self._regex.classify(message)
        logger.debug("Classifier regex: %s", result)
        return result

    # ── xAI Grok path ────────────────────────────────────────────────────────

    async def _classify_via_grok(
        self,
        message: str,
        language: str,
    ) -> IntentResult:
        assert self._groq is not None  # guard: caller checks this before calling
        t0 = time.monotonic()

        user_content = f'Message: "{message}"\nLanguage: {language}'

        response = await self._groq.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": user_content},
            ],
            temperature=0.0,   # deterministic
            max_tokens=120,    # enough for the JSON object
        )

        latency = round((time.monotonic() - t0) * 1000, 1)
        raw = (response.choices[0].message.content or "").strip()

        return self._parse_groq_response(raw, latency_ms=latency)

    @staticmethod
    def _parse_groq_response(raw: str, latency_ms: float = 0.0) -> IntentResult:
        """Parse the JSON returned by Groq into an IntentResult."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Try to extract JSON from surrounding text
            match = re.search(r"\{.*?\}", raw, re.S)
            if match:
                try:
                    data = json.loads(match.group())
                except json.JSONDecodeError:
                    return IntentResult(intent=SEARCH, confidence=0.5, via="fallback")
            else:
                return IntentResult(intent=SEARCH, confidence=0.5, via="fallback")

        intent = str(data.get("intent", SEARCH)).strip().lower()
        if intent not in ALL_INTENTS:
            intent = SEARCH

        confidence = float(data.get("confidence", 0.8))
        confidence = max(0.0, min(1.0, confidence))

        return IntentResult(
            intent=intent,
            confidence=confidence,
            query=str(data.get("query", "") or "").strip(),
            product_ref=str(data.get("product_ref", "") or "").strip(),
            via="groq",
            latency_ms=latency_ms,
        )


# ── Singleton ─────────────────────────────────────────────────────────────────

_classifier_instance: Optional[IntentClassifier] = None


def get_classifier() -> IntentClassifier:
    global _classifier_instance
    if _classifier_instance is None:
        _classifier_instance = IntentClassifier()
    return _classifier_instance
