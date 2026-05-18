"""Redis-backed session state with in-memory fallback."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_MEMORY_STORE: Dict[str, Any] = {}
_MEMORY_MAX = 2000


def _evict_memory_store() -> None:
    if len(_MEMORY_STORE) >= _MEMORY_MAX:
        to_remove = list(_MEMORY_STORE.keys())[: max(1, _MEMORY_MAX // 10)]
        for k in to_remove:
            _MEMORY_STORE.pop(k, None)


class SessionService:
    """Redis-backed session state with in-memory fallback."""

    def __init__(self, redis_client=None, ttl_seconds: int = 7200):
        self.redis = redis_client
        self.ttl_seconds = ttl_seconds

    def _key(self, session_id: str) -> str:
        return f"session:{session_id}"

    def _default_state(self) -> Dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        return {
            "conversation_history": [],
            "cart_snapshot": {},
            "customer_email": None,
            "last_products": [],
            "meta": {},
            "created_at": now,
            "last_active": now,
        }

    async def get_session(self, session_id: str) -> Dict[str, Any]:
        key = self._key(session_id)
        raw = None
        try:
            if self.redis:
                raw = await self.redis.get(key)
            else:
                state = _MEMORY_STORE.get(key)
                return state if isinstance(state, dict) else self._default_state()
        except Exception as exc:
            logger.warning("Session read failed: %s", exc)

        if not raw:
            return self._default_state()
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                return self._default_state()
        except Exception:
            return self._default_state()

        merged = self._default_state()
        merged.update(parsed)
        return merged

    async def update_session(
        self,
        session_id: str,
        *,
        conversation_history: Optional[list] = None,
        cart_snapshot: Optional[Dict[str, Any]] = None,
        customer_email: Optional[str] = None,
        last_products: Optional[list] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        state = await self.get_session(session_id)

        if conversation_history is not None:
            clean = [
                {k: v for k, v in e.items() if v is not None}
                for e in conversation_history[-20:]
                if isinstance(e, dict)
            ]
            state["conversation_history"] = clean

        if cart_snapshot is not None and isinstance(cart_snapshot, dict):
            state["cart_snapshot"] = cart_snapshot

        if customer_email:
            state["customer_email"] = str(customer_email).strip().lower()

        if last_products is not None:
            state["last_products"] = list(last_products)[:12]

        if meta is not None and isinstance(meta, dict):
            current_meta = state.get("meta", {})
            if not isinstance(current_meta, dict):
                current_meta = {}
            state["meta"] = {**current_meta, **meta}

        state["last_active"] = datetime.now(timezone.utc).isoformat()

        key = self._key(session_id)
        encoded = json.dumps(state)
        try:
            if self.redis:
                await self.redis.set(key, encoded, ex=self.ttl_seconds)
            else:
                _evict_memory_store()
                _MEMORY_STORE[key] = state
        except Exception as exc:
            logger.warning("Session write failed: %s", exc)

        return state

    async def get_history(self, session_id: str) -> list:
        state = await self.get_session(session_id)
        return state.get("conversation_history", [])

    async def save_history(self, session_id: str, messages: list) -> None:
        await self.update_session(session_id, conversation_history=messages)

    async def get_customer_email(self, session_id: str) -> Optional[str]:
        state = await self.get_session(session_id)
        value = state.get("customer_email")
        return str(value) if value else None

    async def save_customer_email(self, session_id: str, email: str) -> None:
        await self.update_session(session_id, customer_email=email)

    async def clear_session(self, session_id: str) -> None:
        key = self._key(session_id)
        try:
            if self.redis:
                await self.redis.delete(key)
            else:
                _MEMORY_STORE.pop(key, None)
        except Exception:
            pass

    async def get_meta(self, session_id: str) -> dict:
        key = f"session:{session_id}:meta"
        try:
            if self.redis:
                data = await self.redis.get(key)
                if data:
                    return json.loads(data)
            else:
                return _MEMORY_STORE.get(key, {})
        except Exception as e:
            logger.warning("Meta read failed for %s: %s", session_id, e)
        return {}

    async def save_meta(self, session_id: str, updates: dict) -> None:
        key = f"session:{session_id}:meta"
        try:
            existing = await self.get_meta(session_id)
            merged = {**existing, **updates}
            data = json.dumps(merged, default=str)
            if self.redis:
                await self.redis.setex(key, self.ttl_seconds, data)
            else:
                _evict_memory_store()
                _MEMORY_STORE[key] = merged
        except Exception as e:
            logger.error("Meta save failed for %s: %s", session_id, e)

    async def get_last_products(self, session_id: str) -> list:
        meta = await self.get_meta(session_id)
        return meta.get("last_products", [])

    async def get_language(self, session_id: str) -> str:
        meta = await self.get_meta(session_id)
        return meta.get("language", "en")

    async def save_cart(self, session_id: str, cart: dict) -> None:
        key = f"session:{session_id}:cart"
        try:
            if self.redis:
                await self.redis.setex(key, 3600, json.dumps(cart))
            else:
                _evict_memory_store()
                _MEMORY_STORE[key] = cart
        except Exception as e:
            logger.warning("Cart cache save failed: %s", e)

    async def get_cart(self, session_id: str) -> dict:
        key = f"session:{session_id}:cart"
        try:
            if self.redis:
                data = await self.redis.get(key)
                if data:
                    return json.loads(data)
            else:
                return _MEMORY_STORE.get(key, {})
        except Exception:
            pass
        sym = os.getenv("STORE_CURRENCY", "₹")
        return {"is_empty": True, "items": [], "total": f"{sym}0", "item_count": 0}
