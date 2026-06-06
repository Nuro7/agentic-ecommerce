"""
Consolidated brain decision layer — shared entry point for all voice pipelines.

Both Pipeline A (Gemini Live → ask_brain tool) and Pipeline B (Groq STT → Brain)
call orchestrator.run(), which delegates here. This is the single authoritative
implementation of the full shopping-brain pipeline.

Entry point:  ask_brain(*, session_id, user_message, ..., store_client, session_service)

Pipeline (9 steps):
  1. Input sanitisation + input guardrail
  2. Parallel pre-processing  (asyncio.gather):
       • Intent classify   (Groq LLaMA, ~50 ms)
       • Session load      (Redis, ~5 ms)
       • Session meta load (Redis, ~5 ms)
  3. Language resolution  (detected vs. saved)
  4. Cart fetch           (live store → Redis fallback)
  5. Intent routing:
       OFF_TOPIC  → guardrail rejection
       CHITCHAT   → cached canned response
       STORE_INFO / CART_ACTION / policy intents → fast deterministic handler
       SEARCH / PRODUCT_DETAIL / INVENTORY → retrieval pre-fetch → LLM agent
  6. Post-processing      (strip markup, cap sentences, extract suggestions)
  7. Output guardrail     (hallucination check → stricter-prompt retry)
  8. Schema validation    (AgentResponse Pydantic)
  9. Session persistence + telemetry
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from ...core.security import sanitize_text
from ..prompts.filtering import detect_language, make_speech_friendly
from ..beta_logger import get_beta_logger
from ..llm_clients import ANY_LLM_AVAILABLE
from ..memory.facts import get_session_facts_service
from ..guardrails import (
    check_input, check_output,
    InputBlocked, OutputValidationError, build_retrieved_context,
)
from ..schemas import AgentResponse
from ..retrieval.search import hybrid_search
from ..classifier import (
    get_classifier, IntentResult,
    CHITCHAT, OFF_TOPIC, STORE_INFO, CART_ACTION,
    SEARCH, PRODUCT_DETAIL, INVENTORY,
)
from .canned import normalize_language, chitchat_response, off_topic_response, _OFF_TOPIC_RESPONSES
from .fast_intent import (
    run_fast_intent,
    handle_product_discovery,
    handle_buy_intent,
    handle_availability,
    handle_compare,
    handle_order_tracking,
    handle_add_to_cart,
)
from .llm_loop import run_llm_agent, retry_with_stricter_prompt
from .text_utils import (
    extract_next_suggestions, cap_to_sentences, strip_function_markup,
    summarize_actions_for_voice,
    has_store_info_intent, has_shipping_intent, has_returns_intent,
    has_payment_intent, has_cart_view_intent, has_remove_intent,
)

logger = logging.getLogger(__name__)

# ── Module-level catalog cache ────────────────────────────────────────────────
# Keyed by store base_url (or object id as fallback).
# Shared across all sessions on the same worker process — safe for asyncio
# single-event-loop because dict updates are atomic in CPython.
_catalog_cache: Dict[str, Tuple[str, float]] = {}
_CATALOG_TTL = 300.0  # 5 minutes


# ── Helpers ───────────────────────────────────────────────────────────────────

def _store_key(store_client: Any) -> str:
    return str(getattr(store_client, "base_url", "") or id(store_client))


async def _get_store_catalog(store_client: Any) -> str:
    """Return a short catalog summary string for the system prompt (5-min TTL)."""
    key = _store_key(store_client)
    cached_text, cached_ts = _catalog_cache.get(key, ("", 0.0))
    if cached_text and (time.monotonic() - cached_ts) < _CATALOG_TTL:
        return cached_text

    try:
        parts: List[str] = []

        categories = await store_client.get_categories()
        if categories:
            names = [
                c.get("name", "") for c in categories
                if isinstance(c, dict) and c.get("name") and c.get("count", 1) > 0
            ]
            if names:
                parts.append("Categories: " + ", ".join(names))

        sample = await store_client.search_products(query="", in_stock_only=True, limit=10)
        if sample:
            product_names = list({
                p.get("name", "").split(" – ")[0].strip()
                for p in sample
                if isinstance(p, dict) and p.get("name")
            })[:8]
            if product_names:
                parts.append("Available products include: " + ", ".join(product_names))

        try:
            on_sale = [
                p for p in (sample or [])
                if isinstance(p, dict)
                and p.get("sale_price") and p.get("regular_price")
                and p.get("sale_price") != p.get("regular_price")
            ]
            if on_sale:
                deals = [
                    f"{p.get('name')} (was ₹{p.get('regular_price')}, now ₹{p.get('sale_price')})"
                    for p in on_sale[:3]
                ]
                parts.append("Current deals: " + ", ".join(deals))
        except Exception:
            pass

        catalog = "\n".join(parts)
        if catalog:
            _catalog_cache[key] = (catalog, time.monotonic())
        return catalog

    except Exception as exc:
        logger.debug("Could not pre-fetch store catalog: %s", exc)
        return ""


async def _run_retrieval(
    *,
    query: str,
    tenant_id: str,
    store_client: Any,
    redis: Any,
    db_session_factory: Any,
    limit: int = 5,
) -> List[Any]:
    """Run hybrid retrieval search, owning its db session lifecycle."""
    db = None
    try:
        if db_session_factory is not None:
            db = db_session_factory()
    except Exception:
        pass
    try:
        return await hybrid_search(
            tenant_id=tenant_id,
            query=query,
            redis=redis,
            db=db,
            store_client=store_client,
            limit=limit,
        )
    finally:
        if db is not None:
            try:
                await db.close()
            except Exception:
                pass


async def _fetch_cart(
    session_id: str,
    cart_context: Optional[Dict[str, Any]],
    store_client: Any,
    session_service: Any,
) -> Dict[str, Any]:
    """Return cart dict: prefer cart_context → live store → Redis cache → empty."""
    if cart_context and isinstance(cart_context, dict) and cart_context.get("items"):
        return cart_context
    try:
        cart = await store_client.get_live_cart(session_id=session_id)
        await session_service.save_cart(session_id, cart)
        return cart
    except Exception as exc:
        logger.warning("Live cart fetch failed, using cache: %s", exc)
        cart = await session_service.get_cart(session_id)
        if cart and not cart.get("is_empty", True):
            return cart
        return {"is_empty": True, "items": [], "total": "₹0", "item_count": 0}


# ── Main entry point ──────────────────────────────────────────────────────────

async def ask_brain(
    *,
    session_id: str,
    user_message: str,
    store_context: Dict[str, Any],
    page_context: Dict[str, Any],
    language: str = "en",
    cart_context: Optional[Dict[str, Any]] = None,
    store_client: Any,
    session_service: Any,
    redis: Any = None,
    db_session_factory: Any = None,
) -> Dict[str, Any]:
    """
    Full brain decision pipeline — shared by AgentOrchestrator and both voice pipelines.

    Returns a dict with keys:
      session_id, text, response_text, speech_text, language,
      ui_actions, actions, suggested_replies
    """
    store_context = store_context or {}
    page_context = page_context or {}

    # ── Step 1: Input sanitisation + guardrail ────────────────────────────────
    cleaned_message = sanitize_text(user_message or "", max_len=500)
    try:
        cleaned_message = check_input(cleaned_message)
    except InputBlocked as blocked:
        lang = normalize_language(language)
        off_topic_text = _OFF_TOPIC_RESPONSES.get(lang, _OFF_TOPIC_RESPONSES["en"])
        logger.info("Input blocked (%s): %.60s", blocked.reason, cleaned_message)
        return _blocked_response(session_id, off_topic_text, lang)

    if not cleaned_message:
        text = "Hi! I'm your shopping assistant. How can I help you today?"
        lang = normalize_language(language)
        return _empty_response(session_id, text, lang)

    # ── Step 2: Parallel pre-processing ──────────────────────────────────────
    detected_lang = detect_language(cleaned_message)

    gather_results = await asyncio.gather(
        get_classifier().classify(cleaned_message, detected_lang),
        session_service.get_session(session_id),
        session_service.get_meta(session_id),
        return_exceptions=True,
    )

    intent_result: IntentResult = (
        gather_results[0]
        if not isinstance(gather_results[0], BaseException)
        else IntentResult(intent=SEARCH, confidence=0.5, via="fallback")
    )
    state: Any = gather_results[1] if not isinstance(gather_results[1], BaseException) else {}
    session_meta: Any = gather_results[2] if not isinstance(gather_results[2], BaseException) else {}

    history: List[Dict[str, Any]] = (
        state.get("conversation_history", []) if isinstance(state, dict) else []
    )
    last_products: List[Any] = (
        state.get("last_products", []) if isinstance(state, dict) else []
    )

    # ── Step 3: Language resolution ───────────────────────────────────────────
    prev_lang = session_meta.get("language", "en") if isinstance(session_meta, dict) else "en"
    lang = detected_lang if detected_lang != "en" else prev_lang
    await session_service.save_meta(session_id, {**session_meta, "language": lang})

    logger.debug(
        "Brain: intent=%s conf=%.2f via=%s lang=%s session=%s",
        intent_result.intent, intent_result.confidence, intent_result.via,
        lang, session_id,
    )

    # ── Step 4: Cart fetch ────────────────────────────────────────────────────
    cart = await _fetch_cart(session_id, cart_context, store_client, session_service)
    cart_for_prompt = {
        "is_empty": (int(cart.get("item_count") or cart.get("count") or 0) == 0),
        "item_count": int(cart.get("item_count") or cart.get("count") or 0),
        "total": str(cart.get("total") or "₹0"),
        "items": cart.get("items") or [],
    }

    result: Optional[Dict[str, Any]] = None
    lower_msg = cleaned_message.lower()

    # ── Step 5: Intent routing ────────────────────────────────────────────────
    if intent_result.intent == OFF_TOPIC and intent_result.confidence >= 0.75:
        result = off_topic_response(lang)

    elif intent_result.intent == CHITCHAT and intent_result.confidence >= 0.75:
        result = chitchat_response(lang, session_id)

    elif (
        intent_result.intent in (STORE_INFO, CART_ACTION)
        or has_store_info_intent(lower_msg)
        or has_shipping_intent(lower_msg)
        or has_returns_intent(lower_msg)
        or has_payment_intent(lower_msg)
        or has_cart_view_intent(lower_msg)
        or has_remove_intent(lower_msg)
    ):
        try:
            result = await run_fast_intent(
                cleaned_message, session_id, lang, store_context,
                store_client=store_client, session_service=session_service,
            )
        except Exception as exc:
            logger.warning("Fast-intent pre-LLM failed: %s", exc)

    # ── Retrieval pre-fetch (for search / detail / inventory) ─────────────────
    # retrieval_ran: True only when the DB call completed without exception.
    # retrieval_found: True when the call ran AND returned ≥1 product.
    # We distinguish the two so that a DB/Redis outage does NOT fire the hard
    # stop — the LLM should still get a chance using session history.
    retrieval_ran = False
    retrieval_found = False
    if result is None and intent_result.intent in (SEARCH, PRODUCT_DETAIL, INVENTORY):
        try:
            retrieval_results = await _run_retrieval(
                query=cleaned_message,
                tenant_id=str(store_context.get("tenant_id") or session_id),
                store_client=store_client,
                redis=redis,
                db_session_factory=db_session_factory,
            )
            retrieval_ran = True  # call completed — result may be empty list
            if retrieval_results:
                retrieval_found = True
                last_products = [
                    {
                        "id": r.platform_id,
                        "name": r.name,
                        "price": r.price,
                        "currency": r.currency,
                        "in_stock": r.in_stock,
                        "image_url": r.image_url,
                        "description": r.description[:200] if r.description else "",
                    }
                    for r in retrieval_results
                ]
                logger.debug(
                    "Retrieval: %d products for '%s'",
                    len(last_products), cleaned_message[:40],
                )
            else:
                logger.info(
                    "Retrieval: 0 products for intent=%s query='%s'",
                    intent_result.intent, cleaned_message[:40],
                )
        except Exception as exc:
            logger.warning("Retrieval pre-fetch failed (non-fatal): %s", exc)
            # retrieval_ran stays False — hard stop must NOT fire on infra errors

    # Hard stop: retrieval ran successfully, confirmed zero results, and the
    # session has no prior products to fall back on.
    # Exception: generic browse queries ("what products do you have", "show me everything")
    # should pass through to the LLM so it can call search_products("") to list all items.
    _GENERIC_BROWSE = re.compile(
        r"^(what|which|show|list|see|tell|give).{0,40}"
        r"(product|item|thing|have|stock|sell|offer|catalog|collection)",
        re.I,
    )
    if (
        result is None
        and retrieval_ran
        and not retrieval_found
        and not last_products
        and not _GENERIC_BROWSE.search(cleaned_message)
    ):
        result = _no_products_result(lang)

    # ── Fast specific handlers (pre-LLM, keyword + intent matched) ───────────
    # These deterministic handlers short-circuit the LLM for well-defined
    # patterns where a direct store API call gives the right answer.
    if result is None:
        _order_kw = ("my order", "order status", "track my", "where is my order", "order number", "order tracking")
        _compare_kw = ("compare", " vs ", " versus ", "difference between", "which is better", "which one is")
        _buy_kw = ("i want to buy", "i want to get", "buy me", "get me a", "i'd like to buy", "i'll take", "purchase a")
        _add_kw = ("add to cart", "add it to cart", "put in cart", "add one", "add two", "add three", "add to bag")

        if any(kw in lower_msg for kw in _order_kw):
            try:
                result = await handle_order_tracking(
                    cleaned_message, lower_msg, state if isinstance(state, dict) else {}, lang,
                    store_client=store_client,
                )
            except Exception as exc:
                logger.warning("handle_order_tracking failed: %s", exc)

        if result is None and any(kw in lower_msg for kw in _compare_kw):
            try:
                result = await handle_compare(
                    cleaned_message, lower_msg, last_products, lang,
                    store_client=store_client,
                )
            except Exception as exc:
                logger.warning("handle_compare failed: %s", exc)

        if result is None and intent_result.intent == INVENTORY:
            try:
                result = await handle_availability(
                    cleaned_message, lower_msg, last_products, lang,
                    store_client=store_client,
                )
            except Exception as exc:
                logger.warning("handle_availability failed: %s", exc)

        if result is None and any(kw in lower_msg for kw in _add_kw):
            try:
                result = await handle_add_to_cart(
                    cleaned_message, lower_msg, session_id, last_products, lang,
                    store_client=store_client,
                )
            except Exception as exc:
                logger.warning("handle_add_to_cart failed: %s", exc)

        if result is None and any(kw in lower_msg for kw in _buy_kw):
            try:
                result = await handle_buy_intent(
                    cleaned_message, lower_msg, session_id, lang,
                    store_client=store_client,
                )
            except Exception as exc:
                logger.warning("handle_buy_intent failed: %s", exc)

    # ── Primary: LLM agent ────────────────────────────────────────────────────
    if result is None and ANY_LLM_AVAILABLE:
        try:
            # Retrieval errored (infra down) + no session history → LLM has nothing
            # grounded. Override store_catalog to mandate a live tool call instead of
            # guessing. The string flows into catalog_section of the system prompt via
            # build_system_prompt — no new parameters needed.
            if (
                not retrieval_ran
                and not last_products
                and intent_result.intent in (SEARCH, PRODUCT_DETAIL, INVENTORY)
            ):
                logger.warning(
                    "Retrieval unavailable and no session products — "
                    "mandating live API tool call: session=%s intent=%s",
                    session_id, intent_result.intent,
                )
                store_catalog = (
                    "RETRIEVAL SYSTEM UNAVAILABLE. Your first action MUST be to call "
                    "search_products before saying anything about products, prices, or "
                    "availability. Do not guess, invent, or assume any product information."
                )
            else:
                store_catalog = await _get_store_catalog(store_client)
            result = await run_llm_agent(
                session_id=session_id,
                user_message=cleaned_message,
                store_context=store_context,
                page_context=page_context,
                language=lang,
                cart=cart_for_prompt,
                history=history,
                last_products=last_products,
                cart_context=cart_context if cart_context else cart,
                store_catalog=store_catalog,
                store_client=store_client,
                session_service=session_service,
                redis=redis,
            )
        except Exception as exc:
            logger.warning("LLM agent failed (%s), falling back.", exc)

    # ── Fallback 1: fast-intent ───────────────────────────────────────────────
    if result is None:
        try:
            result = await run_fast_intent(
                cleaned_message, session_id, lang, store_context,
                store_client=store_client, session_service=session_service,
            )
        except Exception as exc:
            logger.warning("Fast-intent fallback failed: %s", exc)

    # ── Fallback 2: product discovery ────────────────────────────────────────
    if result is None:
        try:
            result = await handle_product_discovery(
                cleaned_message, lower_msg, lang,
                store_client=store_client,
            )
        except Exception as exc:
            logger.warning("handle_product_discovery fallback failed: %s", exc)

    if result is None:
        result = {
            "response_text": (
                "What are you looking for? I can help you find products, "
                "check your cart, or answer questions about the store."
            ),
            "ui_actions": [],
            "suggested_replies": ["Show products", "Show my cart", "Store info"],
        }

    # ── Step 6: Post-processing ───────────────────────────────────────────────
    response_text = str(
        result.get("response_text") or "I can help with products, cart, and checkout."
    ).strip()
    ui_actions: List[Dict[str, Any]] = (
        result.get("ui_actions")
        if isinstance(result.get("ui_actions"), list)
        else result.get("actions", [])
    )
    inline_suggestions, response_text = extract_next_suggestions(response_text)
    suggested: List[str] = inline_suggestions or (
        result.get("suggested_replies")
        if isinstance(result.get("suggested_replies"), list)
        else []
    )
    response_text = strip_function_markup(response_text)
    if not response_text:
        response_text = summarize_actions_for_voice(ui_actions)
    if not response_text:
        response_text = "I'm here to help! Ask me about any product."
    response_text = cap_to_sentences(response_text, max_sentences=4)

    # ── Step 7: Output guardrail ──────────────────────────────────────────────
    retrieved_ids: Set[str] = set()
    retrieved_prices: Set[str] = set()
    try:
        retrieved_ids, retrieved_prices, retrieved_attrs = build_retrieved_context(
            [a.get("payload", {}) for a in ui_actions if isinstance(a, dict)]
        )
        response_text = check_output(
            response_text,
            retrieved_product_ids=retrieved_ids or None,
            retrieved_prices=retrieved_prices or None,
            retrieved_attributes=retrieved_attrs or None,
            detected_language=lang,
            allow_retry=True,
        )
    except OutputValidationError as ove:
        logger.warning("Output guardrail triggered (%s) — retrying", ove.reason)
        response_text = await retry_with_stricter_prompt(
            user_message=cleaned_message,
            failure_reason=ove.reason,
            last_products=last_products,
            lang=lang,
            retrieved_ids=retrieved_ids,
            retrieved_prices=retrieved_prices,
        )
    except Exception as exc:
        logger.debug("Output guardrail skipped: %s", exc)

    # ── Step 8: Schema validation ─────────────────────────────────────────────
    try:
        validated = AgentResponse.model_validate({
            "response_text": response_text,
            "ui_actions": ui_actions,
            "suggested_replies": suggested[:5],
            "last_products": last_products,
            "customer_email": result.get("customer_email"),
            "llm_route": result.get("llm_route", "unknown"),
        })
        response_text = validated.response_text or response_text
        ui_actions = validated.ui_actions
        suggested = validated.suggested_replies
    except Exception as exc:
        logger.debug("AgentResponse schema validation skipped: %s", exc)

    speech_text = make_speech_friendly(response_text, lang)

    # ── Step 9: Session persistence + telemetry ───────────────────────────────
    updated_history = (history if isinstance(history, list) else [])[-28:]
    updated_history.extend([
        {"role": "user", "content": cleaned_message},
        {"role": "assistant", "content": response_text},
    ])
    await session_service.update_session(
        session_id,
        conversation_history=updated_history,
        cart_snapshot=cart,
        customer_email=result.get("customer_email"),
        last_products=(
            result.get("last_products")
            if isinstance(result.get("last_products"), list)
            else last_products
        ),
    )

    try:
        facts_payload = [{"result": a.get("payload")} for a in ui_actions if isinstance(a, dict)]
        await get_session_facts_service().update(session_id, cleaned_message, facts_payload)
    except Exception as exc:
        logger.debug("SessionFacts update failed (non-critical): %s", exc)

    try:
        cart_value = float(
            str(cart.get("total") or "0").replace("₹", "").replace(",", "").strip() or "0"
        )
        checkout_reached = any(
            a.get("type") in ("redirect_checkout", "redirect_checkout_with_address")
            for a in ui_actions
            if isinstance(a, dict)
        )
        await get_beta_logger().record_turn(
            session_id=session_id,
            store_id=str(store_context.get("store_id") or store_context.get("url") or ""),
            language=lang,
            llm_route=result.get("llm_route", "gpt-4o-mini"),
            tool_call_count=len(ui_actions),
            cart_value=cart_value,
            checkout_reached=checkout_reached,
        )
    except Exception as exc:
        logger.debug("BetaLogger record failed (non-critical): %s", exc)

    return {
        "session_id": session_id,
        "text": response_text,
        "response_text": response_text,
        "speech_text": speech_text,
        "language": lang,
        "ui_actions": ui_actions,
        "actions": ui_actions,
        "suggested_replies": suggested[:5],
    }


# ── Private response builders ─────────────────────────────────────────────────

def _no_products_result(lang: str) -> Dict[str, Any]:
    """Deterministic response when retrieval finds zero products in the catalog."""
    _texts = {
        "en": "I couldn't find any products matching that in this store. Want me to show you what's available?",
        "hi": "Mujhe is store mein koi matching product nahi mila. Kya main available products dikhau?",
        "ml": "Ith store-il matching products kittunilla. Available products kaanaamo?",
        "ta": "Ithu store-la match aana products kaanala. Available-a irukkara products kaataradha?",
        "te": "Ee store lo match ayye products dorakaledu. Available products chupistha?",
        "bn": "Ei store-e kono matching product pelam na. Available products dekhabo?",
        "kn": "Ee store-alli match aagunna products sigalilla. Available products nodona?",
    }
    text = _texts.get(lang, _texts["en"])
    return {
        "response_text": text,
        "ui_actions": [],
        "suggested_replies": ["Show all products", "Browse categories"],
    }


def _blocked_response(session_id: str, text: str, lang: str) -> Dict[str, Any]:
    speech = make_speech_friendly(text, lang)
    return {
        "session_id": session_id,
        "text": text,
        "response_text": text,
        "speech_text": speech,
        "language": lang,
        "ui_actions": [],
        "actions": [],
        "suggested_replies": ["Show products", "Show my cart", "Help me find something"],
    }


def _empty_response(session_id: str, text: str, lang: str) -> Dict[str, Any]:
    speech = make_speech_friendly(text, lang)
    return {
        "session_id": session_id,
        "text": text,
        "response_text": text,
        "speech_text": speech,
        "language": lang,
        "ui_actions": [],
        "actions": [],
        "suggested_replies": ["Show products", "Show my cart", "Help me find something"],
    }
