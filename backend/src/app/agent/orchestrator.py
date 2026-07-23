"""Agent orchestrator — thin coordinator delegating to brain/core.py."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .memory.session import SessionService
from ..integrations.base.commerce import BaseStoreClient
from .brain import handle_address_collection
from .brain.core import ask_brain

logger = logging.getLogger(__name__)


class AgentOrchestrator:
    def __init__(
        self,
        store_client: BaseStoreClient,
        session_service: SessionService,
        tts_service=None,
        redis=None,
        db_session_factory=None,
    ) -> None:
        self.woo = store_client
        self.session = session_service
        self.tts = tts_service
        self._redis = redis
        self._db_factory = db_session_factory

    # ── Main entry point ──────────────────────────────────────────────────────

    async def run(
        self,
        session_id: str,
        user_message: str,
        store_context: Optional[Dict[str, Any]] = None,
        page_context: Optional[Dict[str, Any]] = None,
        language: str = "en",
        cart_context: Optional[Dict[str, Any]] = None,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        store_context = store_context or {}
        if tenant_id is not None:
            store_context = {**store_context, "tenant_id": tenant_id}
        logger.info("[FLOW] orchestrator.run ENTER session=%s query=%.60s lang=%s", session_id, user_message, language)
        result = await ask_brain(
            session_id=session_id,
            user_message=user_message,
            store_context=store_context,
            page_context=page_context or {},
            language=language,
            cart_context=cart_context,
            store_client=self.woo,
            session_service=self.session,
            redis=self._redis,
            db_session_factory=self._db_factory,
        )
        logger.info("[FLOW] orchestrator.run EXIT session=%s ui_actions=%d text=%.80s", session_id, len(result.get("ui_actions") or []), result.get("response_text", "")[:80])
        return result

    async def handle_address_collection(
        self,
        session_id: str,
        user_message: str,
        current_state: str,
        address_data: dict,
        language: str,
    ) -> tuple:
        return await handle_address_collection(
            session_id, user_message, current_state, address_data, language,
        )
