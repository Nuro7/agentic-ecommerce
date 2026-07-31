"""Pure text-processing utilities for the agent brain (no I/O, no side-effects)."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple


# â”€â”€ Live Shopping Navigator URL helpers (pure, no I/O) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# The assistant drives the real storefront: search page / product page / cart.
# Base URL comes from the widget (store_context["url"]); paths are the
# platform's universal conventions; product URLs are synced permalinks.

def client_platform(store_client) -> str:
    """Best-effort platform from the store client's class name."""
    name = type(store_client).__name__.lower()
    if "woo" in name:
        return "woocommerce"
    if "shopify" in name:
        return "shopify"
    return "custom"


# ── Pure UI / navigation command detection ─────────────────────────────────
# "scroll down", "go to the top", "go home" are PAGE actions, not product
# searches. Gemini Live calls ask_brain() for them and the brain used to treat
# them as searches → "I couldn't find any products matching scroll down".
# Detect them up-front and answer instantly with a short spoken ack; the widget
# performs the actual scroll/navigation locally (handleLocalVoiceCommand), so no
# ui_action is needed for pure scrolls.
_UI_COMMAND_PATTERNS = [
    (re.compile(r"^(?:scroll\s+)?(?:down|downwards?|scroll\s+down)$", re.I), "scroll_down"),
    (re.compile(r"^(?:scroll\s+)?(?:up|upwards?|scroll\s+up)$", re.I), "scroll_up"),
    (re.compile(r"^(?:scroll\s+to\s+|go\s+to\s+|scroll\s+)(?:the\s+)?(?:bottom|end)$", re.I), "scroll_bottom"),
    (re.compile(r"^(?:scroll\s+to\s+|go\s+to\s+|scroll\s+)(?:the\s+)?(?:top|beginning|start)$", re.I), "scroll_top"),
    (re.compile(r"^(?:go\s+to\s+|go\s+|take\s+me\s+to\s+)(?:the\s+)?(?:home|homepage|home\s+page)$", re.I), "home"),
    (re.compile(r"^home(?:page| page)?$", re.I), "home"),
]

_UI_COMMAND_ACK = {
    "scroll_down": "Scrolling down",
    "scroll_up": "Scrolling up",
    "scroll_bottom": "Going to the bottom of the page",
    "scroll_top": "Going to the top of the page",
    "home": "Taking you to the home page",
}


def detect_ui_command_response(
    message: Any,
    store_context: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Return an instant spoken ack for pure UI/navigation commands, else None.

    Skips retrieval and the LLM entirely — these are page actions, not product
    queries. The widget already performs the scroll/navigate locally, so the
    brain only needs to say the right thing quickly (previously it replied
    "couldn't find any products" and added LLM latency).
    """
    text = str(message or "").strip().rstrip(" .!?।")
    if not text or len(text) > 40:
        return None
    for pattern, kind in _UI_COMMAND_PATTERNS:
        if pattern.fullmatch(text):
            actions: List[Dict[str, Any]] = []
            if kind == "home":
                base = str((store_context or {}).get("url") or "").strip()
                if base:
                    actions.append({
                        "type": "redirect",
                        "payload": {"url": base, "reason": "home", "delay_ms": 1500},
                    })
            return {
                "response_text": _UI_COMMAND_ACK.get(kind, ""),
                "ui_actions": actions,
                "suggested_replies": [],
            }
    return None


def storefront_search_url(store_url, platform: str, query) -> Optional[str]:
    """Universal storefront search URL: Shopify /search?q=, Woo /?s=&post_type=product."""
    from urllib.parse import quote_plus
    base = str(store_url or "").strip().rstrip("/")
    q = str(query or "").strip()
    if not base or not q:
        return None
    if platform == "woocommerce":
        return f"{base}/?s={quote_plus(q)}&post_type=product"
    return f"{base}/search?q={quote_plus(q)}"


def product_page_url(product) -> Optional[str]:
    url = str(((product or {}) if isinstance(product, dict) else {}).get("permalink") or "").strip()
    return url or None


def append_live_navigation(
    ui_actions,
    *,
    store_context,
    query,
    platform: str,
    current_url: str = "",
    active_recommendations: Optional[List[Any]] = None,
) -> None:
    """Append ONE `redirect` ui_action matching this turn's answer (in place).

    Priority: add_to_cart → cart page; a single shown product with a permalink
    → its product page; any shown products → the storefront search page with the
    normalized spoken query. Skips when a redirect/checkout action is already
    present, when the target equals the page the customer is on, or when no
    target URL can be built. Additive only — inline cards always still render;
    the widget's live_navigation flag decides whether to actually navigate.

    Ordinal navigation ("first", "second", "that one") targets
    active_recommendations exclusively — never view_history.
    """
    if not isinstance(ui_actions, list):
        return

    ctx = store_context if isinstance(store_context, dict) else {}
    base_url = str(ctx.get("url") or "").strip()
    here = str(current_url or "").strip().rstrip("/")

    def _push(url: str, reason: str, nav_query: str = "", filters: Optional[dict] = None) -> None:
        if not url or url.rstrip("/") == here:
            return
        delay = 0 if reason == "search" else 1500
        payload: Dict[str, Any] = {"url": url, "reason": reason, "delay_ms": delay}
        if nav_query:
            payload["query"] = nav_query
        if filters:
            payload["filters"] = filters
        ui_actions.append({"type": "redirect", "payload": payload})

    lower_query = str(query or "").strip().lower()

    # Product list navigation from recommendations (e.g. "go to the first product")
    # Runs BEFORE the add_to_cart block so ordinal/anaphoric nav takes priority.
    if active_recommendations and isinstance(active_recommendations, list):
        target_index = None
        if re.search(r"\b(first|1st|number one|no 1|no\. 1)\b", lower_query):
            target_index = 0
        elif re.search(r"\b(second|2nd|number two|no 2|no\. 2)\b", lower_query):
            target_index = 1
        elif re.search(r"\b(third|3rd|number three|no 3|no\. 3)\b", lower_query):
            target_index = 2
        elif re.search(r"\b(fourth|4th|number four|no 4|no\. 4)\b", lower_query):
            target_index = 3
        elif re.search(r"\b(fifth|5th|number five|no 5|no\. 5)\b", lower_query):
            target_index = 4
        elif re.search(r"\b(that|this)\s*(one|product|item)?(?:\s*(?:please|thanks|thank\s*you))*\s*$", lower_query) or lower_query in ("take that", "take this", "that one", "this one", "that product", "this product"):
            target_index = 0

        if target_index is not None and len(active_recommendations) > target_index:
            prod = active_recommendations[target_index]
            if isinstance(prod, dict):
                purl = product_page_url(prod)
                if purl:
                    _push(purl, "product")
                    return

    if any(a.get("type") == "add_to_cart" for a in (ui_actions or [])):
        return

    # Profile / Account Page Navigation
    if re.search(r"\b(go to profile|my profile|my account|account settings|view profile|account)\b", lower_query):
        _push((base_url.rstrip("/") if base_url else "") + "/account", "profile")
        return

    # Orders Page Navigation
    if re.search(r"\b(my orders|order history|past orders|my purchases|view orders)\b", lower_query):
        _push((base_url.rstrip("/") if base_url else "") + "/account/orders", "orders")
        return

    # Search Page Navigation
    search_match = re.search(r"\b(search for|search\b|look for|find\b)\s+(.+?)(?:\s*please|\s*thanks|\s*$)", lower_query)
    if search_match:
        raw = search_match.group(2).strip()
        sq, filters = build_searchbar_query(raw)
        if sq:
            sq_url = (base_url.rstrip("/") if base_url else "") + f"/search?q={sq.replace(' ', '+')}"
            _push(sq_url, "search", sq, filters)
        return

    # Home Page Navigation
    if re.search(r"\b(home|homepage|home page|go to home|take me to home|go to the homepage|go to homepage)\b", lower_query):
        _push(base_url or "/", "home")
        return

    # Cart Page Navigation
    if re.search(r"\b(go to cart|show my cart|view cart|open cart|show cart|my cart|take me to my cart|go to my cart)\b", lower_query):
        cart_url = str(ctx.get("cart_url") or "").strip()
        if not cart_url and base_url:
            cart_url = base_url.rstrip("/") + "/cart"
        _push(cart_url or "/cart", "cart")
        return

    # Checkout Page Navigation
    if re.search(r"\b(checkout|go to checkout|proceed to checkout|take me to checkout)\b", lower_query):
        checkout_url = str(ctx.get("checkout_url") or "").strip()
        if not checkout_url and base_url:
            checkout_url = base_url.rstrip("/") + "/checkout"
        _push(checkout_url or "/checkout", "checkout")
        return

    types_present = {a.get("type") for a in ui_actions if isinstance(a, dict)}
    if types_present & {"redirect", "redirect_checkout", "redirect_checkout_with_address"}:
        return  # navigation already decided this turn

    # Collect shown products for single-product redirect
    products = []
    for a in ui_actions:
        if isinstance(a, dict) and a.get("type") == "show_product_detail":
            payload = a.get("payload") or {}
            if payload.get("product"):
                products.append(payload["product"])
    if not products:
        for a in ui_actions:
            if isinstance(a, dict) and a.get("type") == "show_products":
                payload = a.get("payload") or {}
                items = payload.get("products") or []
                products.extend(p for p in items if isinstance(p, dict))
    if not products:
        return

    # Exactly one product with a permalink â†’ its page
    if len(products) == 1:
        purl = product_page_url(products[0])
        if purl:
            _push(purl, "product")
            return

    # Multiple products shown â†’ redirect to the storefront search page
    # with a normalized (conversational filler removed) query + structured filters.
    nav_query, filters = build_searchbar_query(query) if isinstance(products, list) and len(products) > 1 else (query, None)
    if nav_query:
        search_url = storefront_search_url(base_url, platform, nav_query)
        if search_url:
            _push(search_url, "search", nav_query, filters)
            return


# â”€â”€ Query normalisation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def normalize_discovery_query(message: str) -> str:
    # Strip apostrophes so contractions (i'll, i'm, etc.) don't interfere with \b
    cleaned = message.lower().replace("'", " ").replace("`", " ")
    # Leading greetings ("hi", "hello") are not search terms
    cleaned = re.sub(r"^\s*(?:hi|hello|hey|yo|good\s+(?:morning|afternoon|evening))\b[\s,]*", " ", cleaned, flags=re.IGNORECASE)
    # 1) Price/currency phrases BEFORE the stopword pass — "under 500" must be
    #    removed as a unit. Otherwise the stopword regex drops "under" first and
    #    the bare "500" leaks into the storefront searchbar query.
    cleaned = re.sub(r"\b(?:under|below|less\s+than|above|over|more\s+than|upto|up\s+to|max|maximum)\s+\d+(?:\.\d+)?\b", " ", cleaned)
    cleaned = re.sub(r"\b(?:rs|inr|usd|\$|₹|€|£|dollars?|rupees?|pounds?|euros?|bucks?)\s*\d+(?:\.\d+)?\b", " ", cleaned)
    cleaned = re.sub(r"\b\d+(?:\.\d+)?\s*(?:rs|inr|usd|\$|₹|€|£|dollars?|rupees?|pounds?|euros?|bucks?)\b", " ", cleaned)
    cleaned = re.sub(r"\b(?:rs|inr|usd|\$|₹|€|£|dollars?|rupees?|pounds?|euros?|bucks?)\b", " ", cleaned)
    cleaned = re.sub(r"[₹$€£]", " ", cleaned)
    # Strip size keywords from search query (they are passed as structured filters)
    # Handles both "size 9" and "my size is 9" phrasing.
    cleaned = re.sub(r"\b(?:size|sized?)\s*(?:uk|us|eu)?\s*(?:is|are|of)?\s*\d+(?:\.\d+)?\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(?:size|sized?)\s+(?:uk|us|eu)\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(xxs?|xs|s|m|l|xl|xxl|xxxl|2xl|3xl|small|medium|large|xsmall|xsm|xlarge)\b", " ", cleaned, flags=re.IGNORECASE)
    # Strip colour words + colour filler from search query (structured `color` filter)
    cleaned = re.sub(
        r"\b(?:\b(?:black|white|red|blue|green|yellow|orange|purple|pink|brown|grey|gray|tan|beige|navy|maroon|gold|silver|olive|teal|indigo|violet|cream|ivory|charcoal|crimson|magenta|turquoise|cyan|wine|mauve|rust|bordeaux|mustard|offwhite|multicolor|multicolour|colorful)\b|colour|color)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    # 2) Stopword pass (filler/occasion words are not storefront search terms)
    cleaned = re.sub(
        r"\b(?:"
        r"show|find|search|products?|items?|available|availability|"
        r"compare|cart|checkout|please|l(?:oo)?king\s+for|"
        r"list|that|this|those|these|the|a|an|"
        r"want|am|is|are|was|were|do|does|can|have|has|there|any|which|tell|me|about|"
        r"you|in|stock|check|for|what|see|some|my|your|his|"
        r"her|our|their|its|brother|sister|mother|father|friend|wife|husband|"
        r"son|daughter|birthday|anniversary|wedding|party|gift|present|"
        r"surprise|tomorrow|today|yesterday|need|would|could|should|will|"
        r"shall|may|might|must|just|only|also|very|really|quite|all|both|"
        r"each|every|no|nor|not|none|neither|either|with|without|from|into|"
        r"onto|upon|than|then|now|here|there|where|when|why|how|well|too|"
        r"such|as|at|by|to|of|on|off|up|down|out|over|under|again|further|"
        r"once|which|while|who|whom|what|whether|because|since|after|before|"
        r"until|during|through|between|among|across|behind|beyond|within|"
        r"along|around|about|above|below|near|past|toward|towards|via|"
        r"without|worth|yet|so|but|or|and|nor|if|though|although|unless|"
        r"except|thanks|thank|add|he|she|it|we|they|him|them|office|home|"
        r"house|work|school|college|i|love|prefer|like|favourite|favorite"
        r")\b",
        " ", cleaned
    )
    return re.sub(r"\s+", " ", cleaned).strip()


def build_searchbar_query(message: str) -> tuple[str, dict]:
    """Split a discovery utterance into (searchbar query, structured filters).

    The searchbar gets the clean core query ("formal shoes"); price/size/colour/
    occasion are extracted as structured filters so they NEVER leak into the
    storefront searchbar text (e.g. "under 500" must not become "...shoes 500").
    """
    from ..retrieval.normalizer import normalize

    nq = normalize(message or "")
    filters: dict = {}
    if nq.min_price is not None:
        filters["min_price"] = nq.min_price
    if nq.max_price is not None:
        filters["max_price"] = nq.max_price
    if nq.size:
        filters["size"] = nq.size
    if nq.color:
        filters["color"] = nq.color
    if nq.occasion:
        filters["occasion"] = nq.occasion
    if nq.in_stock_only:
        filters["in_stock_only"] = True
    return normalize_discovery_query(message), filters


def normalize_availability_query(message: str) -> str:
    cleaned = re.sub(r"\b(do you have|is|available|availability|in stock|stock|size\s*[a-z0-9.-]+|check)\b", " ", message.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def extract_add_query(message: str) -> str:
    cleaned = re.sub(r"\b(add|to|cart|please|qty|quantity|size\s*[a-z0-9.-]+|color\s*[a-z-]+|my|the|in|into)\b", " ", message.lower())
    cleaned = re.sub(r"[\"']", " ", cleaned)
    cleaned = re.sub(r"\b\d+\b", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def split_compare_terms(message: str) -> List[str]:
    lower = message.lower()
    if " versus " in lower:
        parts = lower.split(" versus ")
    elif " vs " in lower:
        parts = lower.split(" vs ")
    else:
        parts = lower.replace("compare", "").split(" and ")
    out: List[str] = []
    for part in parts:
        item = re.sub(r"\s+", " ", part).strip(" ,.-")
        if item:
            out.append(item)
    return out[:3]


# â”€â”€ Extraction helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def extract_budget(lower: str) -> Tuple[Optional[float], Optional[float]]:
    max_match = re.search(r"(?:under|below|less\s+than|upto|up\s+to|max|maximum|budget|cheap)\s*[₹$€£]?\s*(\d+(?:\.\d+)?)", lower)
    if not max_match:
        max_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:dollars?|rupees?|rs|inr|usd|eur|gbp|pounds?|euros?|bucks?|\$|₹)", lower)
    min_match = re.search(r"(?:above|over|more\s+than|min|minimum|starting\s+from|from)\s*[₹$€£]?\s*(\d+(?:\.\d+)?)", lower)
    return (
        float(min_match.group(1)) if min_match else None,
        float(max_match.group(1)) if max_match else None,
    )


def extract_quantity(lower: str) -> int:
    qty_pattern = re.search(
        r'\b(\d{1,2})\s*(?:piece|pcs|qty|quantity|units?|nos?|number|pairs?|sets?)\b'
        r'|(?:buy|add|get|want|need|take|order)\s+(\d{1,2})\b',
        lower,
    )
    if qty_pattern:
        val = int(qty_pattern.group(1) or qty_pattern.group(2))
        if 1 <= val <= 20:
            return val
    return 1


def extract_size_color(lower: str) -> Tuple[Optional[str], Optional[str]]:
    size_match = re.search(r"\b(?:size|sized?)\s*([a-z0-9.\-]+)", lower)
    color_match = re.search(r"\b(?:color|colour|in)\s+([a-z]+)\b", lower)

    _SIZES = {"xs", "s", "m", "l", "xl", "xxl", "xxxl", "2xl", "3xl",
               "small", "medium", "large", "xsmall", "xsm", "xlarge"}
    _COLORS = {"red", "blue", "green", "black", "white", "yellow", "pink",
               "orange", "purple", "grey", "gray", "gold", "silver", "brown",
               "navy", "maroon", "violet", "cyan", "beige", "cream", "khaki"}

    size = size_match.group(1).strip() if size_match else None
    color = color_match.group(1).strip() if color_match else None

    if not size:
        for w in lower.split():
            if w in _SIZES:
                size = w
                break
    if not color:
        for w in lower.split():
            if w in _COLORS:
                color = w
                break

    return size, color


def extract_email(lower: str) -> Optional[str]:
    match = re.search(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", lower)
    return match.group(0) if match else None


def speech_digits_to_ascii(text: str) -> str:
    value = str(text or "").lower()
    digit_words = {
        "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
        "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    }
    for word, digit in digit_words.items():
        value = re.sub(rf"\b{word}\b", digit, value)
    value = value.translate(str.maketrans("à¥¦à¥§à¥¨à¥©à¥ªà¥«à¥¬à¥­à¥®à¥¯", "0123456789"))
    return value


def normalize_india_state(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    normalized = re.sub(r"\s+", " ", raw).lower().strip()
    mapping = {
        "andhra pradesh": "AP", "arunachal pradesh": "AR", "assam": "AS",
        "bihar": "BR", "chhattisgarh": "CG", "goa": "GA", "gujarat": "GJ",
        "haryana": "HR", "himachal pradesh": "HP", "jharkhand": "JH",
        "karnataka": "KA", "kerala": "KL", "madhya pradesh": "MP",
        "maharashtra": "MH", "manipur": "MN", "meghalaya": "ML",
        "mizoram": "MZ", "nagaland": "NL", "odisha": "OR", "orissa": "OR",
        "punjab": "PB", "rajasthan": "RJ", "sikkim": "SK", "tamil nadu": "TN",
        "telangana": "TS", "tripura": "TR", "uttar pradesh": "UP",
        "uttarakhand": "UK", "west bengal": "WB", "delhi": "DL",
        "jammu and kashmir": "JK", "ladakh": "LA", "puducherry": "PY",
    }
    if normalized in mapping:
        return mapping[normalized]
    if re.fullmatch(r"[a-zA-Z]{2}", raw):
        return raw.upper()
    return raw


# â”€â”€ Type-safe coercions â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def safe_optional_int(value: Any) -> Optional[int]:
    try:
        if value in (None, "", 0, "0"):
            return None
        return int(value)
    except Exception:
        return None


def safe_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


# â”€â”€ Intent detection â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def should_use_llm(message: str) -> bool:
    message_lower = message.lower()
    context_words = {
        'it', 'that', 'this', 'those', 'these', 'ones',
        'woh', 'yeh', 'iska', 'uska', 'pehla', 'doosra',
        'athu', 'ithu', 'avar', 'ivan', 'avan',
    }
    words = set(message_lower.split())
    if words & context_words:
        return True
    context_phrases = [
        'the first one', 'the second one', 'the third one', 'the last one',
        'that one', 'this one', 'add it', 'add that', 'the same',
        'similar one', 'that product', 'the red one', 'the blue one',
        'the cheap one', 'the expensive one', 'the other one', 'both of them',
        'the first', 'the second', 'the third',
    ]
    if any(phrase in message_lower for phrase in context_phrases):
        return True
    if re.search(r'\b(add|buy|take|get)\s+\d+\b', message_lower):
        if not re.search(r'(product|item|piece|unit)', message_lower):
            return True
    return False


def has_buy_intent(lower: str) -> bool:
    return any(token in lower for token in [
        "i want to buy", "i'd like to buy", "want to buy",
        "i want to purchase", "i'd like to purchase", "want to purchase",
        "i want to order", "i'll take", "get me a", "buy me a",
    ])


def has_buy_now_intent(lower: str) -> bool:
    return any(token in lower for token in [
        "buy now", "buy this now", "purchase now", "order now",
        "buy it now", "get it now", "buy this", "buy it",
    ])


def has_add_intent(lower: str) -> bool:
    if any(token in lower for token in [
        "add to cart", "add this to cart", "add it to cart",
        "buy this", "yes add", "put in cart", "add one",
        "add it", "add this", "yes, add",
    ]):
        return True
    if re.search(r"\b(take|get|pick|select|choose|grab)\s+(the\s+)?(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th|that|this)\b", lower):
        return True
    return False


def has_remove_intent(lower: str) -> bool:
    return any(token in lower for token in [
        "remove", "delete from cart", "delete item", "delete product", "delete this",
    ])


def has_cart_view_intent(lower: str) -> bool:
    return any(token in lower for token in [
        "show cart", "my cart", "view cart", "cart total", "open cart",
    ]) or lower.strip() == "cart"


# "go to cart" / "take me to the cart" / "open the cart page" â†’ NAVIGATE the
# storefront to the real cart page (not just render it inline). Kept separate
# from has_cart_view_intent so "show my cart" still renders inline. This must be
# checked in the brain's fast-intent gate (core.py) too â€” otherwise a classifier
# that mislabels "go to the cart" as SEARCH sends it to product retrieval and the
# LLM hallucinates ("I can't access the cart") instead of navigating.
_CART_NAV_RE = re.compile(
    r"\b(go to|take me to|open|navigate to|bring me to|send me to)\b[\w\s]{0,15}\bcart\b"
    r"|\bcart page\b",
    re.IGNORECASE,
)


def has_cart_nav_intent(lower: str) -> bool:
    return bool(_CART_NAV_RE.search(lower))


def has_clear_cart_intent(lower: str) -> bool:
    return any(token in lower for token in [
        "clear cart", "clear my cart", "clear all",
        "empty cart", "empty my cart",
        "remove all", "remove all items", "delete all",
        "remove everything", "cart ko khaali karo",
    ])


def has_quantity_intent(lower: str) -> bool:
    return bool(re.search(
        r"\b(increase|decrease|reduce|change|update|adjust|"
        r"add more|add another|add one more|"
        r"quantity|qty|double|halve|"
        r"make it|set to)\b", lower,
    )) and any(w in lower for w in ["cart", "item", "product", "quantity", "qty"])


def has_checkout_intent(lower: str) -> bool:
    return any(token in lower for token in [
        "checkout", "proceed to checkout", "buy now", "place order", "order now",
    ])


def has_compare_intent(lower: str) -> bool:
    return "compare" in lower or " vs " in lower or " versus " in lower


def has_inventory_intent(lower: str) -> bool:
    catalog_query = bool(
        re.search(r"(show|list|what|which).*(available).*(product|products)", lower)
        or re.search(r"available\s+products?", lower)
    )
    if catalog_query:
        return False
    return any(token in lower for token in [
        "availability", "in stock", "stock", "size ", "do you have",
    ])


def has_order_intent(lower: str) -> bool:
    tracking_tokens = [
        "track my order", "where is my order", "order status", "my order",
        "track order", "order tracking", "order delivered", "order shipped",
    ]
    if any(token in lower for token in tracking_tokens):
        return True
    purchase_words = [
        "want to order", "want to buy", "i'll order", "i will order",
        "place order", "order now", "order a ", "order the ",
    ]
    if any(w in lower for w in purchase_words):
        return False
    return False


def has_store_info_intent(lower: str) -> bool:
    tokens = [
        "store info", "store name", "shop name", "what is this store",
        "what's this store", "what are store name", "what is store name",
        "name of store", "who are you", "about this store", "store details",
        "about store", "store information", "tell me about", "about your shop",
        "about the shop", "about the store", "shop info", "shop details",
    ]
    if any(token in lower for token in tokens):
        return True
    return bool(re.search(r"\b(store|shop)\b.*\bname\b", lower))


def has_shipping_intent(lower: str) -> bool:
    tokens = [
        "delivery charge", "delivery cost", "delivery fee", "delivery price",
        "shipping charge", "shipping cost", "shipping fee", "shipping price",
        "shipping policy", "delivery policy",
        "how much delivery", "how much shipping", "how much for delivery",
        "free delivery", "free shipping",
        "delivery time", "shipping time", "how long delivery", "how long shipping",
        "do you deliver", "do you ship",
    ]
    return any(t in lower for t in tokens)


def has_returns_intent(lower: str) -> bool:
    tokens = [
        "return policy", "returns policy", "refund policy",
        "can i return", "can i exchange", "how to return", "how to refund",
        "return product", "exchange product",
        "return period", "return window", "money back",
        "what is your return", "what is the return",
    ]
    return any(t in lower for t in tokens)


def has_payment_intent(lower: str) -> bool:
    tokens = [
        "payment method", "payment option", "how to pay", "how can i pay",
        "accepted payment", "pay online", "pay by card", "pay by upi",
        "do you accept", "cash on delivery", "cod", "credit card", "debit card",
    ]
    return any(t in lower for t in tokens)


# â”€â”€ Response building â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def with_actions_alias(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    ui = payload.get("ui_actions")
    if "actions" not in payload and isinstance(ui, list):
        payload["actions"] = ui
    return payload


def normalize_cart_payload(cart: Dict[str, Any]) -> Dict[str, Any]:
    item_count = int(cart.get("item_count") or cart.get("count") or 0)
    return {
        "is_empty": item_count == 0,
        "item_count": item_count,
        "total": str(cart.get("total") or "â‚¹0"),
        "items": cart.get("items") or [],
    }


def in_stock(row: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(row, dict):
        return False
    status = str(row.get("stock_status", "")).lower().strip()
    if status:
        return status in ("instock", "onbackorder")
    if isinstance(row.get("is_in_stock"), bool):
        return bool(row.get("is_in_stock"))
    if isinstance(row.get("in_stock"), bool):
        return bool(row.get("in_stock"))
    return True


def pick_best_product_match(query: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {}
    terms = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 1]
    if not terms:
        return rows[0]
    best = rows[0]
    best_score = -1
    needle = query.lower().strip()
    for row in rows:
        name = str(row.get("name", "")).lower()
        score = sum(2 for term in terms if term in name)
        if needle and name.startswith(needle):
            score += 3
        if score > best_score:
            best_score = score
            best = row
    return best


# â”€â”€ LLM response text processing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def extract_next_suggestions(text: str) -> Tuple[List[str], str]:
    """Extract NEXT: suggestion line from LLM response."""
    if not text:
        return [], text
    pattern = re.compile(r'\n?NEXT\s*:\s*(.+)$', re.IGNORECASE | re.MULTILINE)
    match = pattern.search(text)
    suggestions: List[str] = []
    if match:
        raw = match.group(1)
        suggestions = [s.strip() for s in raw.split("|") if s.strip()][:3]
        text = pattern.sub("", text).strip()
    return suggestions, text


def cap_to_sentences(text: str, max_sentences: int = 4) -> str:
    if not text:
        return text
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) <= max_sentences:
        return text
    truncated = " ".join(parts[:max_sentences])
    if truncated and truncated[-1] not in ".!?":
        truncated += "."
    return truncated


def strip_function_markup(text: str) -> str:
    cleaned = str(text or "")
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"<reasoning>.*?</reasoning>", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"<function\s*=\s*([a-zA-Z0-9_]+)\s*({.*?})\s*</function>", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"<function[^>]*>", "", cleaned, flags=re.DOTALL)
    cleaned = cleaned.replace("</function>", "")
    cleaned = re.sub(r'\b[a-zA-Z_][a-zA-Z0-9_]*\s*\(\s*\{[^}]{0,300}\}\s*\)', "", cleaned)
    _TOOL_KEYS = r'(?:query|product_id|product_ids|category|limit|min_price|max_price|in_stock_only|cart_item_key|attributes|quantity|coupon_code|email|compare_ids|order_id)'
    cleaned = re.sub(r'\{[^{}]{0,400}' + _TOOL_KEYS + r'[^{}]{0,400}\}', "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def summarize_actions_for_voice(actions: List[Dict[str, Any]]) -> str:
    if not actions:
        return "I can help with products, availability, cart, and checkout."

    for action in actions:
        if not isinstance(action, dict):
            continue
        action_type = str(action.get("type") or "")
        payload = action.get("payload", {}) if isinstance(action.get("payload"), dict) else {}
        if action_type == "show_availability":
            product = payload.get("product", {}) if isinstance(payload.get("product"), dict) else {}
            inventory = payload.get("inventory", {}) if isinstance(payload.get("inventory"), dict) else {}
            name = str(product.get("name") or "That product")
            if inventory.get("variant_not_found"):
                return f"That exact size or color isn't available for {name}. I can show you what options are available."
            if inventory.get("in_stock"):
                qty = inventory.get("stock_quantity")
                qty_text = f" â€” only {qty} left" if isinstance(qty, int) and qty > 0 else ""
                return f"{name} is in stock{qty_text}. Want me to add it to your cart?"
            return f"{name} is currently out of stock. Want me to show similar options?"
        if action_type == "show_variants":
            product = payload.get("product", {})
            name = str(product.get("name") or "this product")
            return f"I've shown the available options for {name}. Please select your size and quantity, then tap Add to Cart."
        if action_type == "add_to_cart":
            return "Adding that to your cart now."
        if action_type == "show_cart":
            cart = payload.get("cart", {}) if isinstance(payload.get("cart"), dict) else {}
            count = int(cart.get("item_count") or cart.get("count") or 0)
            total = str(cart.get("total") or "â‚¹0")
            return f"Your cart has {count} items. Total is {total}."
        if action_type == "show_orders":
            return "I found your recent order details."

    for action in actions:
        if not isinstance(action, dict):
            continue
        action_type = str(action.get("type") or "")
        payload = action.get("payload", {}) if isinstance(action.get("payload"), dict) else {}
        if action_type == "show_products":
            products = payload.get("products", []) if isinstance(payload.get("products"), list) else []
            if products:
                name = str(products[0].get("name") or "")
                price = str(products[0].get("price") or "")
                price_text = f", â‚¹{price}" if price else ""
                return f"{name}{price_text}. Take a look â€” let me know which one you like."
            return "Couldn't find a match. Try a different product name or budget?"

    return "I completed that request. Tell me what you want to do next."


# â”€â”€ Inline function call extractor â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_KNOWN_TOOLS = frozenset({
    "search_products", "get_product_details", "check_inventory",
    "add_to_cart", "add_multiple_to_cart", "remove_from_cart", "get_cart",
    "get_orders", "apply_coupon", "get_categories", "get_store_info",
    "compare_products", "get_reviews", "find_variants", "get_best_coupon",
    "update_cart_quantity", "submit_review",
})


def extract_inline_function_calls(content: str) -> Tuple[List[Tuple[str, Dict[str, Any]]], str]:
    if not content:
        return [], ""

    calls: List[Tuple[str, Dict[str, Any]]] = []
    cleaned = content

    # Pattern 1: <function=name {...}></function>
    xml_pattern = re.compile(r"<function\s*=\s*([a-zA-Z0-9_]+)\s*({.*?})\s*</function>", re.DOTALL)
    for match in xml_pattern.finditer(content):
        name = (match.group(1) or "").strip()
        args_raw = (match.group(2) or "{}").strip()
        if not name:
            continue
        try:
            args = json.loads(args_raw)
            if not isinstance(args, dict):
                args = {}
        except Exception:
            args = {}
        calls.append((name, args))
    cleaned = xml_pattern.sub("", cleaned)
    cleaned = re.sub(r"</?function[^>]*>", "", cleaned)
    cleaned = re.sub(r"<function\s*=\s*[a-zA-Z0-9_]+\s*{.*?}>", "", cleaned, flags=re.DOTALL)

    # Pattern 2: function_name({"key": "value"})
    py_pattern = re.compile(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(\s*(\{[^}]{0,400}\})\s*\)')
    for match in py_pattern.finditer(cleaned):
        name = (match.group(1) or "").strip()
        if name not in _KNOWN_TOOLS:
            continue
        args_raw = (match.group(2) or "{}").strip()
        try:
            args = json.loads(args_raw)
            if not isinstance(args, dict):
                args = {}
        except Exception:
            args = {}
        calls.append((name, args))
    cleaned = py_pattern.sub(
        lambda m: "" if m.group(1) in _KNOWN_TOOLS else m.group(0),
        cleaned,
    )

    # Pattern 3: {"type": "function", "name": "tool_name", "parameters"|"arguments": {...}}
    type_fn_pattern = re.compile(
        r'\{\s*"type"\s*:\s*"function"\s*,\s*"name"\s*:\s*"([a-zA-Z0-9_]+)"\s*(?:,\s*"(?:parameters|arguments)"\s*:\s*(\{[^{}]*\}))?\s*\}',
        re.DOTALL,
    )
    for match in type_fn_pattern.finditer(cleaned):
        name = match.group(1).strip()
        if name not in _KNOWN_TOOLS:
            continue
        try:
            args = json.loads(match.group(2)) if match.group(2) else {}
            if not isinstance(args, dict):
                args = {}
        except Exception:
            args = {}
        calls.append((name, args))
    cleaned = type_fn_pattern.sub(
        lambda m: "" if m.group(1) in _KNOWN_TOOLS else m.group(0),
        cleaned,
    )

    # Pattern 4: {"name": "tool_name", "arguments": {...}}
    name_args_pattern = re.compile(
        r'\{\s*"name"\s*:\s*"([a-zA-Z0-9_]+)"\s*,\s*"arguments"\s*:\s*(\{[^{}]*\})\s*\}',
        re.DOTALL,
    )
    for match in name_args_pattern.finditer(cleaned):
        name = match.group(1).strip()
        if name not in _KNOWN_TOOLS:
            continue
        try:
            args = json.loads(match.group(2))
            if not isinstance(args, dict):
                args = {}
        except Exception:
            args = {}
        calls.append((name, args))
    cleaned = name_args_pattern.sub(
        lambda m: "" if m.group(1) in _KNOWN_TOOLS else m.group(0),
        cleaned,
    )

    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return calls, cleaned


def bind_highlight_target(response_text: str, ui_actions: List[Dict[str, Any]]) -> None:
    """Pillar 4 — bind highlight_card to the product the reply actually references.

    The LLM answers "the BATA UK 9 looks great" while the search tool highlighted
    the first in-stock card (usually index 0), so the glow lands on the wrong shoe.
    This scans every show_products action, resolves which product the spoken text
    points at (via an explicit "ID: <n>" reference or a unique full-name match), and
    rewrites highlight_card.target_index to that product's array index. Single-product
    lists and unknown references are left untouched (target_index stays as produced).
    """
    if not ui_actions or not response_text:
        return

    shown: List[Dict[str, Any]] = []
    for a in ui_actions:
        if not isinstance(a, dict):
            continue
        if a.get("type") != "show_products":
            continue
        payload = a.get("payload", {})
        items = payload.get("products") if isinstance(payload, dict) else None
        if isinstance(items, list):
            shown.extend(p for p in items if isinstance(p, dict))
    if len(shown) < 2:
        return

    lowered = response_text.lower()

    # Explicit "ID: <n>" reference (same format the output guardrail validates).
    pid_refs = [m for m in re.findall(r"\bID\s*[:\s]\s*(\d+)\b", response_text, re.IGNORECASE)]
    target_index: Optional[int] = None
    for pid in pid_refs:
        for i, p in enumerate(shown):
            if str(p.get("id") or p.get("product_id") or "") == pid:
                target_index = i
                break
        if target_index is not None:
            break

    # Unique leading-word mention: product names carry " - Color Size" suffixes,
    # so exact full-name substring fails for "the Woodland Boots are great".
    # Match on the product's first significant words (skipping color/size tokens)
    # appearing contiguously in the reply.
    if target_index is None:
        _SKIP = {
            "the", "a", "an", "men", "men's", "women", "women's", "kids", "for", "with",
            "uk", "us", "eu", "size", "color", "colour",
            "black", "white", "red", "blue", "green", "yellow", "orange", "purple",
            "pink", "brown", "grey", "gray", "beige", "gold", "silver", "navy",
            "maroon", "teal", "cream", "tan", "olive", "burgundy",
        }
        exact_matches: List[int] = []
        for i, p in enumerate(shown):
            words = re.findall(r"[a-z0-9]+", str(p.get("name") or "").lower())
            words = [w for w in words if w not in _SKIP and not w.isdigit()]
            lead = words[:3]
            if not lead:
                continue
            needle = " ".join(lead)
            if needle in lowered:
                exact_matches.append(i)
        if len(exact_matches) == 1:
            target_index = exact_matches[0]

    if target_index is None:
        # No grounded product reference in the reply — drop any ungrounded
        # highlight so the glow can never land on an arbitrary default index
        # (e.g. a hardcoded target_index: 0).
        ui_actions[:] = [
            a for a in ui_actions
            if not (isinstance(a, dict) and a.get("type") == "highlight_card")
        ]
        return

    # Rewrite the first highlight_card action (or add one next to show_products).
    highlight_action = next(
        (a for a in ui_actions if isinstance(a, dict) and a.get("type") == "highlight_card"),
        None,
    )
    if highlight_action is not None:
        payload = highlight_action.get("payload")
        if isinstance(payload, dict):
            payload["target_index"] = target_index
    else:
        for a in ui_actions:
            if isinstance(a, dict) and a.get("type") == "show_products":
                ui_actions.append({
                    "type": "highlight_card",
                    "payload": {"target_index": target_index, "products": a.get("payload", {}).get("products", [])},
                })
                break
