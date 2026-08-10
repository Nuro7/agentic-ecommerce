"""Natural-language offer ingestion — POST /offers/from-text.

Converts a merchant's plain-English offer ("20% off Nike shoes for Diwali",
"buy 2 get 1 free on Watches", "flat 500 off above 2 qty") into structured
ProductOfferCreate data.

Strategy (deterministic-first):
    1. If an LLM is available (gpt-4o-mini), ask it to extract the offer as
       strict JSON against the same shape the schema expects.
    2. Always fall back to regex heuristics so the endpoint works offline too.
    3. The result is validated by ProductOfferCreate before persistence.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from .schemas import ProductOfferCreate

logger = logging.getLogger(__name__)

_LLM_TOOL = {
    "type": "function",
    "function": {
        "name": "extract_offer",
        "description": "Extract a merchant promotion into a structured JSON offer.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short marketing title for the offer.",
                },
                "description": {
                    "type": "string",
                    "description": "Plain description of the offer.",
                },
                "offer_kind": {
                    "type": "string",
                    "enum": ["discount", "dead_stock", "combo", "bulk"],
                },
                "offer_type": {
                    "type": "string",
                    "enum": ["promotion", "dead_stock", "new_arrival", "seasonal"],
                },
                "platform_id": {
                    "type": "string",
                    "description": "Product platform_id if a single product is targeted.",
                },
                "product_name": {
                    "type": "string",
                    "description": "Product name if a single product is targeted.",
                },
                "discount_percent": {
                    "type": "number",
                    "description": "Percent discount (0-100) for discount/dead_stock kinds.",
                },
                "discount_amount": {
                    "type": "number",
                    "description": "Fixed amount discount.",
                },
                "combo_items": {
                    "type": "array",
                    "description": "Items in a combo bundle.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "platform_id": {"type": "string"},
                            "quantity": {"type": "integer"},
                            "name": {"type": "string"},
                        },
                    },
                },
                "combo_price": {
                    "type": "number",
                    "description": "Bundled price when combo items are bought together.",
                },
                "bulk_tiers": {
                    "type": "array",
                    "description": "Quantity tiers ordered ascending by min_qty.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "min_qty": {"type": "integer"},
                            "discount_percent": {"type": "number"},
                            "discount_amount": {"type": "number"},
                        },
                    },
                },
                "max_redemptions": {"type": "integer"},
                "priority": {"type": "integer"},
            },
            "required": ["title", "offer_kind"],
        },
    },
}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _try_num(match: Optional[re.Match], group: int = 1) -> Optional[float]:
    if not match or not match.group(group):
        return None
    raw = match.group(group).replace(",", "")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _parse_percent(text: str) -> Optional[float]:
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    val = _try_num(m)
    if val is not None and 0 <= val <= 100:
        return val
    return None


def _parse_amount(text: str) -> Optional[float]:
    m = re.search(r"(?:flat|rs\.?|inr|₹|rs)\s*([\d,]+(?:\.\d+)?)", text, re.IGNORECASE)
    return _try_num(m)


def _parse_bulk_tiers(text: str) -> Optional[List[Dict[str, Any]]]:
    """Detect tiered qty discounts: '2+ => 10%', 'qty 3 gets 15%', 'min 2 -> 500 off'."""
    tiers: List[Dict[str, Any]] = []
    pattern = re.compile(
        r"(?:min\.?\s*|minimum\s+)?(?:qty\s+)?(\d+)"
        r"(?:\s*\+\s*(?:qty\s+)?|\s+(?:qty|items|pcs)\b)?"
        r"\s*(?:=>|→|->|:-|:|gets?|for)\s*"
        r"(\d{1,3}(?:\.\d+)?\s*%|[\d,]+(?:\.\d+)?(?:\s*(?:off|rs\.?|₹))?)",
        re.IGNORECASE,
    )
    for m in pattern.finditer(text):
        min_qty = int(m.group(1))
        raw_disc = m.group(2)
        tier: Dict[str, Any] = {"min_qty": min_qty}
        pct = re.match(r"(\d{1,3}(?:\.\d+)?)\s*%", raw_disc)
        if pct:
            tier["discount_percent"] = float(pct.group(1))
        else:
            amt = re.search(r"([\d,]+(?:\.\d+)?)", raw_disc)
            val = _try_num(amt)
            if val is None:
                continue
            tier["discount_amount"] = val
        tiers.append(tier)
    if not tiers:
        return None
    tiers.sort(key=lambda t: int(t.get("min_qty", 0)))
    return tiers


def _parse_combo(text: str) -> Optional[Dict[str, Any]]:
    """Detect 'buy X get Y (for price)' bundles.

    Handles the shapes:
      "buy 2 get 1 free on Watches"
      "buy Shoe + Watch combo at 999"
    """
    items: List[Dict[str, Any]] = []
    buy_get = re.search(
        r"buy\s+(\d+)\s*(?:get\s+(\d+))?\s*(?:free|for\s+([\d,]+(?:\.\d+)?)?)?\s+on\s+(.+)",
        text,
        re.IGNORECASE,
    )
    if buy_get:
        buy_qty = int(buy_get.group(1))
        get_qty = int(buy_get.group(2) or buy_get.group(1) or 1)
        name = buy_get.group(4).strip()
        items = [
            {"platform_id": "", "quantity": buy_qty, "name": name},
            {"platform_id": "", "quantity": get_qty, "name": name},
        ]
        combo_price = _try_num(buy_get, 3)
        return {"items": items, "price": combo_price}

    plus = re.search(
        (
            r"([A-Z][A-Za-z0-9 ]*?)\s*\+\s*([A-Z][A-Za-z0-9 ]*?)"
            r"\s+(?:at|for|combo|bundle|price)\s*:?\s*([\d,]+(?:\.\d+)?)"
        ),
        text,
        re.IGNORECASE,
    )
    if plus:
        first = plus.group(1).strip()
        second = plus.group(2).strip()
        first = re.sub(r"^buy\s+", "", first, flags=re.IGNORECASE).strip()
        if first and second and " " not in first:
            items = [
                {"platform_id": "", "quantity": 1, "name": first},
                {"platform_id": "", "quantity": 1, "name": second},
            ]
            return {"items": items, "price": _try_num(plus, 3)}
    return None


async def _extract_via_llm(text: str) -> Optional[Dict[str, Any]]:
    """Structured extraction via gpt-4o-mini. Returns a raw offer dict or None."""
    try:
        from ...agent.llm_router import gpt_mini_client, _call_gpt_mini
        if gpt_mini_client is None:
            return None
        messages = [
            {
                "role": "system",
                "content": (
                    "You convert a merchant's promotion text into one strict JSON offer. "
                    "Output ONLY the tool call extract_offer with exact fields. "
                    "If the text mentions no specific product, leave platform_id empty. "
                    "For 'buy X get Y free', set offer_kind=combo, combo_items with quantities, "
                    "and combo_price if a bundle price is stated (0 or null when free). "
                    "For quantity tiers like '2+ => 10%', set offer_kind=bulk with "
                    "bulk_tiers ascending by min_qty."
                ),
            },
            {"role": "user", "content": text},
        ]
        result = await _call_gpt_mini(messages, [_LLM_TOOL])
        for tc in result.get("tool_calls") or []:
            if tc.get("name") == "extract_offer":
                args = tc.get("arguments") or {}
                if isinstance(args, str):
                    args = json.loads(args)
                return args
        # Fall back to raw JSON text if the model echoed it instead of a tool call.
        content = result.get("text") or ""
        if content.strip().startswith("{"):
            return json.loads(content)
        return None
    except Exception as exc:
        logger.debug("LLM offer extraction failed (falling back to regex): %s", exc)
        return None


def _merge(data: Dict[str, Any], fallback: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in fallback.items():
        if value is not None and data.get(key) in (None, "", []):
            data[key] = value
    return data


async def parse_offer_from_text(text: str) -> Dict[str, Any]:
    """Parse a merchant's offer text into ProductOfferCreate-compatible data.

    Deterministic heuristics always run; the LLM result is merged on top only
    where the heuristics left a field empty. Never raises.
    """
    cleaned = _clean(text)
    title = cleaned[:200] or "Merchant offer"

    fallback: Dict[str, Any] = {"title": title, "offer_kind": "discount"}
    pct = _parse_percent(cleaned)
    amt = _parse_amount(cleaned)
    if pct is not None:
        fallback["discount_percent"] = pct
    if amt is not None and pct is None:
        fallback["discount_amount"] = amt

    tiers = _parse_bulk_tiers(cleaned)
    if tiers:
        fallback["offer_kind"] = "bulk"
        fallback["bulk_tiers"] = tiers

    combo = _parse_combo(cleaned)
    if combo:
        fallback["offer_kind"] = "combo"
        fallback["combo_items"] = combo["items"]
        fallback["combo_price"] = combo["price"]

    data: Dict[str, Any] = {}
    llm_data = await _extract_via_llm(cleaned)
    if llm_data:
        data = {k: v for k, v in llm_data.items() if v is not None}
        data = _merge(data, fallback)
    else:
        data = fallback

    # Coerce / sanitize before schema validation
    if not data.get("title"):
        data["title"] = cleaned[:200]
    if data.get("offer_kind") == "combo":
        for item in data.get("combo_items") or []:
            item["platform_id"] = str(item.get("platform_id") or "")
        if not data.get("combo_price"):
            data["combo_price"] = 0.0
    if data.get("offer_kind") == "bulk":
        data["bulk_tiers"] = sorted(
            data.get("bulk_tiers") or [],
            key=lambda t: int(t.get("min_qty", 0) or 0),
        )
    if not data.get("offer_kind"):
        data["offer_kind"] = "discount"

    # Strip unknown keys so ProductOfferCreate never rejects extra fields.
    allowed = set(ProductOfferCreate.model_fields.keys())
    return {k: v for k, v in data.items() if k in allowed}
