"""
Per-session customer preference facts (size, color, budget, last product).
Stored in Redis hash with 2h TTL; falls back to in-process dict.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

_SIZE_RE = re.compile(
    r"\b(XXS|XS|S\b|M\b|L\b|XL|XXL|XXXL|[0-9]{1,3}\s*(?:cm|mm|in|inch|\"|\'))\b",
    re.IGNORECASE,
)
_COLOR_WORDS = {
    "red", "blue", "green", "yellow", "black", "white", "pink", "purple",
    "orange", "grey", "gray", "brown", "beige", "cream", "navy", "maroon",
    "gold", "silver", "cyan", "magenta", "violet", "indigo", "teal",
    "lal", "neela", "hara", "kala", "safed", "gulabi", "peela",
}
_BUDGET_RE = re.compile(
    r"(?:under|below|less\s+than|max(?:imum)?|budget[:\s]+|upto?|within)\s*"
    r"(?:rs\.?|₹|inr)?\s*([0-9][0-9,]*)",
    re.IGNORECASE,
)
_BUDGET_PLAIN_RE = re.compile(r"(?:rs\.?|₹|inr)\s*([0-9][0-9,]*)", re.IGNORECASE)
SESSION_FACTS_TTL = 7_200

# ── Coarse product-category detector ──────────────────────────────────────────
# Size/colour/last-product preferences are product-specific. When the customer
# switches category (shoes → laptops), the OLD "size M / red" must NOT keep being
# injected into the new context — that was the "topic switch mismatch" symptom.
_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "footwear": ("shoe", "shoes", "sneaker", "sneakers", "boot", "boots",
                 "sandal", "sandals", "heel", "heels", "slipper", "slippers"),
    "apparel": ("shirt", "t-shirt", "tshirt", "tee", "dress", "jeans", "pant",
                "pants", "trouser", "trousers", "kurta", "kurti", "saree",
                "jacket", "hoodie", "top", "skirt", "lehenga", "salwar"),
    "electronics": ("laptop", "phone", "mobile", "smartphone", "headphone",
                    "headphones", "earbud", "earbuds", "earphone", "tv",
                    "camera", "tablet", "watch", "charger", "speaker", "monitor"),
    "beauty": ("lipstick", "cream", "perfume", "shampoo", "makeup", "serum",
               "lotion", "fragrance", "moisturizer"),
    "bags": ("bag", "backpack", "wallet", "purse", "handbag", "luggage"),
    "jewelry": ("ring", "necklace", "earring", "earrings", "bracelet", "pendant"),
}

# Product-specific facts dropped on a category switch (budget is left intact —
# a stated budget usually still applies across categories).
_TOPIC_SCOPED_FACTS = ("preferred_size", "preferred_color",
                       "last_product_id", "last_product_name")


def _detect_category(message: str) -> Optional[str]:
    lower = message.lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if re.search(rf"\b{re.escape(kw)}\b", lower):
                return category
    return None


class SessionFactsService:
    def __init__(self, redis_client=None):
        self._r = redis_client
        self._mem: dict[str, dict[str, Any]] = {}

    async def update(self, tenant_id: str, session_id: str, user_message: str, tool_results: Optional[list[dict]] = None) -> None:
        extracted = _extract_facts(user_message, tool_results or [])
        new_category = _detect_category(user_message)
        current = await self.get(tenant_id, session_id)
        changed = False

        # Topic switch → drop product-specific prefs so old size/colour/product
        # don't bleed into the new category.
        prev_category = current.get("category")
        if new_category and prev_category and new_category != prev_category:
            for k in _TOPIC_SCOPED_FACTS:
                if current.pop(k, None) is not None:
                    changed = True
            logger.debug(
                "SessionFacts topic switch %s→%s — cleared product prefs (session=%s)",
                prev_category, new_category, session_id,
            )
        if new_category and new_category != prev_category:
            current["category"] = new_category
            changed = True

        for k, v in extracted.items():
            if v is not None and current.get(k) != v:
                current[k] = v
                changed = True

        if changed:
            await self._save(tenant_id, session_id, current)

    async def get(self, tenant_id: str, session_id: str) -> dict[str, Any]:
        return await self._load(tenant_id, session_id)

    def format_for_prompt(self, facts: dict[str, Any]) -> str:
        if not facts:
            return ""
        parts = []
        if facts.get("preferred_size"):
            parts.append(f"size preference: {facts['preferred_size']}")
        if facts.get("preferred_color"):
            parts.append(f"color preference: {facts['preferred_color']}")
        if facts.get("max_budget"):
            parts.append(f"budget ≤ ₹{facts['max_budget']}")
        if facts.get("last_product_name"):
            parts.append(f"last discussed: {facts['last_product_name']}")
        return ("Customer preferences — " + ", ".join(parts) + ".") if parts else ""

    def _redis_key(self, tenant_id: str, session_id: str) -> str:
        return f"session_facts:{tenant_id}:{session_id}"

    async def _load(self, tenant_id: str, session_id: str) -> dict[str, Any]:
        mem_key = f"{tenant_id}:{session_id}"
        if self._r is not None:
            try:
                raw = await self._r.get(self._redis_key(tenant_id, session_id))
                if raw:
                    return json.loads(raw)
            except Exception as e:
                logger.debug("SessionFacts Redis GET failed: %s", e)
        return dict(self._mem.get(mem_key, {}))

    async def _save(self, tenant_id: str, session_id: str, facts: dict[str, Any]) -> None:
        if self._r is not None:
            try:
                await self._r.setex(
                    self._redis_key(tenant_id, session_id),
                    SESSION_FACTS_TTL,
                    json.dumps(facts, ensure_ascii=False),
                )
            except Exception as e:
                logger.debug("SessionFacts Redis SET failed: %s", e)
        self._mem[f"{tenant_id}:{session_id}"] = facts


def _extract_facts(message: str, tool_results: list[dict]) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    msg_lower = message.lower()

    m = _SIZE_RE.search(message)
    if m:
        facts["preferred_size"] = m.group(0).upper().strip()

    for word in msg_lower.split():
        clean = re.sub(r"[^\w]", "", word)
        if clean in _COLOR_WORDS:
            facts["preferred_color"] = clean
            break

    m = _BUDGET_RE.search(msg_lower)
    if m:
        facts["max_budget"] = int(m.group(1).replace(",", ""))
    elif not facts.get("max_budget"):
        m = _BUDGET_PLAIN_RE.search(msg_lower)
        if m:
            facts["max_budget"] = int(m.group(1).replace(",", ""))

    for result in tool_results:
        data = result.get("content") or result.get("result") or {}
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                continue
        if isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                if first.get("id"):
                    facts["last_product_id"] = first["id"]
                if first.get("name"):
                    facts["last_product_name"] = first["name"]
        elif isinstance(data, dict):
            if data.get("id"):
                facts["last_product_id"] = data["id"]
            if data.get("name"):
                facts["last_product_name"] = data["name"]

    return facts


_instance: Optional[SessionFactsService] = None


def get_session_facts_service(redis_client=None) -> SessionFactsService:
    global _instance
    if _instance is None:
        _instance = SessionFactsService(redis_client=redis_client)
    elif redis_client is not None and _instance._r is None:
        _instance._r = redis_client
    return _instance
