"""Pure text-processing utilities for the agent brain (no I/O, no side-effects)."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple


# ── Live Shopping Navigator URL helpers (pure, no I/O) ───────────────────────
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
) -> None:
    """Append ONE `redirect` ui_action matching this turn's answer (in place).

    Priority: add_to_cart → cart page; a single shown product with a permalink
    → its product page; any shown products → the storefront search page with the
    normalized spoken query. Skips when a redirect/checkout action is already
    present, when the target equals the page the customer is on, or when no
    target URL can be built. Additive only — inline cards always still render;
    the widget's live_navigation flag decides whether to actually navigate.
    """
    if not isinstance(ui_actions, list):
        return
    types_present = {a.get("type") for a in ui_actions if isinstance(a, dict)}
    if types_present & {"redirect", "redirect_checkout", "redirect_checkout_with_address"}:
        return  # navigation already decided this turn

    ctx = store_context if isinstance(store_context, dict) else {}
    base_url = str(ctx.get("url") or "").strip()
    here = str(current_url or "").strip().rstrip("/")

    def _push(url: str, reason: str, nav_query: str = "") -> None:
        if not url or url.rstrip("/") == here:
            return
        payload: Dict[str, Any] = {"url": url, "reason": reason, "delay_ms": 1500}
        if nav_query:
            payload["query"] = nav_query
        ui_actions.append({"type": "redirect", "payload": payload})

    # 1. Item just added → cart page
    if "add_to_cart" in types_present:
        cart_url = str(ctx.get("cart_url") or "").strip()
        if not cart_url and base_url:
            cart_url = base_url.rstrip("/") + "/cart"
        _push(cart_url, "cart")
        return

    # Collect shown products
    products = []
    for a in ui_actions:
        if isinstance(a, dict) and a.get("type") in ("show_products", "show_product_detail"):
            payload = a.get("payload") or {}
            items = payload.get("products") or ([payload["product"]] if payload.get("product") else [])
            products.extend(p for p in items if isinstance(p, dict))
    if not products:
        return

    # 2. Exactly one product with a permalink → its page
    if len(products) == 1:
        purl = product_page_url(products[0])
        if purl:
            _push(purl, "product")
            return

    # 3. Multiple results → storefront search reflecting the spoken requirement
    nav_q = normalize_discovery_query(str(query or "")) or str(query or "").strip()
    _push(storefront_search_url(base_url, platform, nav_q) or "", "search", nav_q)


# ── Query normalisation ───────────────────────────────────────────────────────

def normalize_discovery_query(message: str) -> str:
    cleaned = re.sub(
        r"\b(show|find|search|products?|items?|available|availability|compare|cart|checkout|please|i need|i want|looking for|list|what are|that|this|those|these|the|a|an|give me|get me|want|is|are|do|does|can|have|has|there|any|which|tell|me|about|you|in|stock|check|do you|looking|for|what|which|see|any|some)\b",
        " ",
        message.lower(),
    )
    cleaned = re.sub(r"\b(under|below|less than|above|over|more than)\s*\d+(?:\.\d+)?\b", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


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


# ── Extraction helpers ────────────────────────────────────────────────────────

def extract_budget(lower: str) -> Tuple[Optional[float], Optional[float]]:
    max_match = re.search(r"(?:under|below|less than|upto|up to)\s*(\d+(?:\.\d+)?)", lower)
    min_match = re.search(r"(?:above|over|more than)\s*(\d+(?:\.\d+)?)", lower)
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
    value = value.translate(str.maketrans("०१२३४५६७८९", "0123456789"))
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


# ── Type-safe coercions ───────────────────────────────────────────────────────

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


# ── Intent detection ──────────────────────────────────────────────────────────

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


def has_add_intent(lower: str) -> bool:
    return any(token in lower for token in [
        "add to cart", "add this to cart", "add it to cart",
        "buy this", "yes add", "put in cart", "add one",
        "add it", "add this", "yes, add",
    ])


def has_remove_intent(lower: str) -> bool:
    return any(token in lower for token in [
        "remove", "delete from cart", "delete item", "delete product", "delete this",
    ])


def has_cart_view_intent(lower: str) -> bool:
    return any(token in lower for token in [
        "show cart", "my cart", "view cart", "cart total", "open cart",
    ]) or lower.strip() == "cart"


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


# ── Response building ─────────────────────────────────────────────────────────

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
        "total": str(cart.get("total") or "₹0"),
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


# ── LLM response text processing ─────────────────────────────────────────────

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
                qty_text = f" — only {qty} left" if isinstance(qty, int) and qty > 0 else ""
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
            total = str(cart.get("total") or "₹0")
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
                price_text = f", ₹{price}" if price else ""
                return f"{name}{price_text}. Take a look — let me know which one you like."
            return "Couldn't find a match. Try a different product name or budget?"

    return "I completed that request. Tell me what you want to do next."


# ── Inline function call extractor ────────────────────────────────────────────

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
