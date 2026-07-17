"""Public widget endpoints — no auth required."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ...agent.prompts.filtering import detect_language
from ...agent.voice.transcription import STTService
from ...config import settings
from ...core.database import get_db
from ...core.ratelimit import rate_limit
from ...modules.billing.dependencies import check_conversation_quota
from ...modules.billing.service import BillingService
from ...modules.tenants.dependencies import (
    DEV_TENANT_ID,
    get_tenant_store_client,
    resolve_tenant_id_from_request,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["widget"])


class GreetCurrentPage(BaseModel):
    url: Optional[str] = None
    title: Optional[str] = None
    product_id: Optional[int] = None
    product_name: Optional[str] = None


class GreetRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    store_name: Optional[str] = None
    language: Optional[str] = "auto"
    current_page: Optional[GreetCurrentPage] = None


def _normalize_language(value: Optional[str]) -> str:
    raw = str(value or "").strip().lower()
    supported = {"en", "hi", "ml", "ta", "te", "bn", "kn", "gu", "pa"}
    if raw in supported:
        return raw
    for lang in supported:
        if raw.startswith(lang):
            return lang
    return "en"


def _cart_summary(cart: Dict[str, Any]) -> Dict[str, Any]:
    count = int(cart.get("item_count") or cart.get("count") or 0)
    total = str(cart.get("total") or cart.get("cart_total") or f"{settings.store_currency}0")
    return {"item_count": count, "total": total, "is_empty": count <= 0}


# ── Speech-to-text (tap-to-speak voice input) ────────────────────────────────
# The browser records audio and POSTs it here; we transcribe it and return the
# text, which the widget then sends through the normal chat flow. This is the
# plan-agnostic voice-INPUT path (distinct from the Pro-gated Gemini Live voice).
_stt_service: STTService | None = None


def _get_stt() -> STTService:
    global _stt_service
    if _stt_service is None:
        _stt_service = STTService()
    return _stt_service


@router.post("/transcribe")
async def transcribe_endpoint(
    audio: UploadFile = File(...),
    session_id: str = Form(""),   # widget sends it; kept for API contract
    language: str = Form(""),
    _rl=Depends(rate_limit(limit=30, window=60, scope="transcribe")),
):
    audio_bytes = await audio.read()
    if not audio_bytes:
        return {"transcript": "", "confidence": 0.0, "language": "en"}
    mime = audio.content_type or "audio/webm"
    lang = str(language or "").strip().lower()
    lang_hint = _normalize_language(language) if lang and lang != "auto" else ""
    try:
        transcript, confidence, detected = await _get_stt().transcribe(
            audio_bytes, mime, language_hint=lang_hint
        )
    except Exception as exc:
        logger.warning("Transcription failed: %s", exc)
        return {"transcript": "", "confidence": 0.0, "language": lang_hint or "en"}
    return {
        "transcript": transcript or "",
        "confidence": float(confidence or 0.0),
        "language": detected or lang_hint or "en",
    }


@router.post("/greet")
async def greet_endpoint(
    payload: GreetRequest,
    req: Request,
    store_client: Any = Depends(get_tenant_store_client),
    _rl=Depends(rate_limit(limit=20, window=60, scope="greet")),
    _quota=Depends(check_conversation_quota),
    db: AsyncSession = Depends(get_db),
):
    session_service = getattr(req.app.state, "session_service", None)
    tts_service = getattr(req.app.state, "tts_service", None)

    session_id = payload.session_id
    store_name = payload.store_name or "this store"

    # Resolve tenant once: real id (or None) for billing; key-safe id for session keys.
    resolved_tid = await resolve_tenant_id_from_request(req, db)
    tenant_id = resolved_tid or DEV_TENANT_ID

    session_meta = await session_service.get_meta(tenant_id, session_id) if session_service else {}
    history = await session_service.get_history(tenant_id, session_id) if session_service else []

    if str(payload.language or "").strip().lower() == "auto":
        if session_meta.get("language"):
            language = _normalize_language(session_meta.get("language"))
        else:
            accept_lang = req.headers.get("accept-language", "")
            language = detect_language(accept_lang)
    else:
        language = _normalize_language(payload.language)

    greeted_before = bool(session_meta.get("greeted"))
    is_returning = greeted_before or len(history) > 0

    cart: Dict[str, Any] = {}
    if store_client:
        try:
            cart = await store_client.get_cart_for_session(session_id)
        except Exception as exc:
            logger.warning("Greet cart fetch failed: %s", exc)

    cart_summary = _cart_summary(cart)
    has_cart = not cart_summary["is_empty"]

    product_context = ""
    if payload.current_page and payload.current_page.product_id and store_client:
        try:
            product = await store_client.get_product_details(int(payload.current_page.product_id))
            product_name = str(product.get("name") or payload.current_page.product_name or "").strip()
            product_price = str(product.get("price") or "").strip()
            if product_name:
                product_context = product_name
                if product_price:
                    product_context += f", priced at {product_price}"
        except Exception as exc:
            logger.info("Greet product context fetch failed: %s", exc)

    greetings = {
        "en": {
            "new_general": f"Hey, welcome to {store_name}! I'm Aria, your shopping assistant. What are you looking for today?",
            "new_product": f"Hey! I see you're looking at {product_context}. Want me to tell you more about it, or check if it's available in your size?",
            "returning_no_cart": "Hey, welcome back! Good to see you again. What can I help you find today?",
            "returning_with_cart": f"Hey, welcome back! You've got {cart_summary['item_count']} items in your cart totalling {cart_summary['total']}. Want to pick up where you left off, or are you looking for something new?",
        },
        "hi": {
            "new_general": f"Namaste! {store_name} mein aapka swagat hai. Main Aria hoon, aapka shopping assistant. Aaj kya dhundhne mein help karoon?",
            "new_product": f"Namaste! Aap {product_context} dekh rahe hain. Kya main iske baare mein aur bataaon, ya size availability check karoon?",
            "returning_no_cart": "Hey, welcome back! Aaj kya dekhna hai?",
            "returning_with_cart": f"Welcome back! Aapke cart mein {cart_summary['item_count']} items hain, total {cart_summary['total']}. Checkout karein ya kuch aur dekhein?",
        },
        "ml": {
            "new_general": f"Namaskaram! {store_name} il swagatham. Njaan Aria, ningalude shopping assistant. Innu enthu thedi varunu?",
            "new_product": f"Namaskaram! Ningal {product_context} nokkunund. Ine patri koodi ariyano, allenkil size undо ennu nokatte?",
            "returning_no_cart": "Swagatham! Innu enthu venam?",
            "returning_with_cart": f"Swagatham! Ningalude cart-il {cart_summary['item_count']} items undu, total {cart_summary['total']}. Checkout cheyyano, allenkil shopping continue cheyyano?",
        },
        "ta": {
            "new_general": f"Vanakkam! {store_name} ku varaverkirom. Naan Aria, ungal shopping assistant. Inru enna thedugirirkal?",
            "new_product": f"Vanakkam! Neenga {product_context} paarkkirirkal. Ine pattri kodumai sollaattuma, illai size irukkaa endra paarkkaattuma?",
            "returning_no_cart": "Vanakkam! Inru enna thedugirirkal?",
            "returning_with_cart": f"Vanakkam! Ungal cart-il {cart_summary['item_count']} items ullathu, total {cart_summary['total']}. Checkout seyyungala, illai shopping continue seyyungala?",
        },
    }

    lang_greetings = greetings.get(language, greetings["en"])
    if is_returning:
        key = "returning_with_cart" if has_cart else "returning_no_cart"
    elif product_context:
        key = "new_product"
    else:
        key = "new_general"
    greeting_text = lang_greetings.get(key, lang_greetings["new_general"])

    # Merchant-customized greeting (tenant column, migration 0016) overrides the
    # default first-visit greeting only. Returning-customer and product-context
    # greetings stay dynamic — a static custom message can't mention the cart or
    # the product being viewed.
    if key == "new_general":
        from ...modules.tenants.service import get_store_config_for_tenant
        _cfg = await get_store_config_for_tenant(tenant_id)
        if _cfg.get("greeting_message"):
            greeting_text = _cfg["greeting_message"]

    audio_b64 = None
    if tts_service:
        try:
            save_meta_coro = (
                session_service.save_meta(tenant_id, session_id, {**session_meta, "greeted": True, "language": language})
                if session_service else asyncio.sleep(0)
            )
            audio_b64, _ = await asyncio.gather(
                tts_service.synthesize(greeting_text, language=language),
                save_meta_coro,
            )
        except Exception as exc:
            logger.warning("TTS synthesis failed: %s", exc)
    elif session_service:
        await session_service.save_meta(tenant_id, session_id, {**session_meta, "greeted": True, "language": language})

    suggested_replies_map = {
        "en": (
            ["Continue checkout", "Show my cart", "Find products"]
            if has_cart else ["Show best sellers", "Search products", "Store info"]
        ),
        "hi": (
            ["Checkout karo", "Mera cart dekho", "Products dikhao"]
            if has_cart else ["Best sellers dikhao", "Products search karo", "Store ki info"]
        ),
        "ml": (
            ["Checkout cheyyuka", "Ente cart kanuka", "Products theduka"]
            if has_cart else ["Best sellers kanuka", "Products search cheyyuka", "Store info"]
        ),
        "ta": (
            ["Checkout seyyungal", "En cart paarungal", "Products thedungal"]
            if has_cart else ["Best sellers paarungal", "Products thedungal", "Store info"]
        ),
    }
    suggested_replies = suggested_replies_map.get(language, suggested_replies_map["en"])

    # Credit recording uses the REAL resolved tenant (resolved at the top); the dev
    # sentinel is never billed.
    if resolved_tid:
        try:
            await BillingService(db).record_usage(resolved_tid, "credits", 1)
            await db.commit()
        except Exception as exc:
            logger.warning("Failed to record greet credit: tenant=%s: %s", resolved_tid, exc)

    return {
        "session_id": session_id,
        "greeting_text": greeting_text,
        "audio_base64": audio_b64,
        "audio_format": tts_service.audio_format() if (tts_service and audio_b64) else None,
        "tts_fallback": "browser" if not audio_b64 else None,
        "language_detected": language,
        "language": language,
        "has_cart": has_cart,
        "cart_summary": ({"item_count": cart_summary["item_count"], "total": cart_summary["total"]} if has_cart else None),
        "is_returning": is_returning,
        "suggested_replies": suggested_replies,
    }


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2000)
    language: Optional[str] = "auto"
    store_name: Optional[str] = None
    store_url: Optional[str] = None
    cart_context: Optional[Dict[str, Any]] = None
    current_page: Optional[GreetCurrentPage] = None


@router.post("/chat")
async def chat_endpoint(
    payload: ChatRequest,
    req: Request,
    store_client: Any = Depends(get_tenant_store_client),
    _rl=Depends(rate_limit(limit=30, window=60, scope="chat")),
    _quota=Depends(check_conversation_quota),
    db: AsyncSession = Depends(get_db),
):
    """Typed-text chat over plain HTTP → the Brain (ask_brain).

    Typed messages used to be forced through the voice WebSocket (Gemini Live),
    which chats instead of searching and drops/reconnects (latency + lost turns).
    This endpoint runs the Brain directly and deterministically, with no voice
    dependency. Voice keeps using the WS; text uses this.
    """
    from ...agent.orchestrator import AgentOrchestrator
    from ...core.database import AsyncSessionLocal

    session_service = getattr(req.app.state, "session_service", None)
    redis = getattr(req.app.state, "redis", None)

    if str(payload.language or "").strip().lower() == "auto":
        language = detect_language(payload.message)
    else:
        language = _normalize_language(payload.language)

    # Resolve tenant_id (mirrors greet) so retrieval hits this tenant's cache.
    from ...modules.tenants.repository import TenantRepository
    tenant_id: Optional[str] = None
    try:
        _repo = TenantRepository(db)
        _shop = req.query_params.get("shop", "").strip()
        if _shop:
            _t = await _repo.get_by_shopify_domain(_shop)
            if _t:
                tenant_id = _t.id
        if not tenant_id:
            _hdr = req.headers.get("X-Tenant-ID", "").strip()
            if _hdr:
                _t = await _repo.get_by_id(_hdr)
                if _t and _t.is_active:
                    tenant_id = _t.id
        if not tenant_id:
            _qp = req.query_params.get("tenant_id", "").strip()
            if _qp:
                _t = await _repo.get_by_id(_qp)
                if _t and _t.is_active:
                    tenant_id = _t.id
    except Exception:
        pass

    # Per-tenant currency (tenant DB column → global env fallback).
    from ...modules.tenants.service import get_store_config_for_tenant
    _store_cfg = await get_store_config_for_tenant(tenant_id)

    store_context = {
        "store_name": payload.store_name or _store_cfg.get("store_name") or "this store",
        "currency_symbol": _store_cfg.get("currency_symbol") or settings.store_currency,
        "tenant_id": tenant_id,
        "url": payload.store_url or "",
    }
    cp = payload.current_page
    page_context: Dict[str, Any] = (
        {"url": cp.url, "title": cp.title, "product_id": cp.product_id, "product_name": cp.product_name}
        if cp else {}
    )

    orchestrator = AgentOrchestrator(
        store_client=store_client,
        session_service=session_service,
        redis=redis,
        db_session_factory=AsyncSessionLocal,
    )
    try:
        result = await orchestrator.run(
            session_id=payload.session_id,
            user_message=payload.message,
            store_context=store_context,
            page_context=page_context,
            language=language,
            cart_context=payload.cart_context,
        )
    except Exception as exc:
        logger.error("Chat endpoint Brain error session=%s: %s", payload.session_id, exc, exc_info=True)
        return {
            "session_id": payload.session_id,
            "text": "Sorry, I had trouble with that. Could you try again?",
            "response_text": "Sorry, I had trouble with that. Could you try again?",
            "language": language,
            "ui_actions": [], "actions": [], "suggested_replies": [],
        }

    text = result.get("response_text") or result.get("text") or ""
    acts = result.get("ui_actions") or result.get("actions") or []
    resp_lang = result.get("language") or language
    speech_text = result.get("speech_text") or text

    # Synthesize TTS so EVERY reply speaks — not just the opening greeting. The
    # widget only plays audio when `audio_base64` is present (browser-TTS fallback
    # was removed), and /greet was the only endpoint returning it. Mirror it here.
    audio_b64 = None
    tts_service = getattr(req.app.state, "tts_service", None)
    if tts_service and speech_text:
        try:
            audio_b64 = await tts_service.synthesize(speech_text, language=resp_lang)
        except Exception as exc:
            logger.warning("Chat TTS synthesis failed session=%s: %s", payload.session_id, exc)

    return {
        "session_id": payload.session_id,
        "text": text,
        "response_text": text,
        "speech_text": speech_text,
        "language": resp_lang,
        "ui_actions": acts,
        "actions": acts,
        "suggested_replies": result.get("suggested_replies") or [],
        "audio_base64": audio_b64,
        "audio_format": tts_service.audio_format() if (tts_service and audio_b64) else None,
    }


@router.get("/cart")
async def get_cart(
    session_id: str,
    store_client: Any = Depends(get_tenant_store_client),
    _rl=Depends(rate_limit(limit=60, window=60, scope="cart")),
):
    """
    Public cart fetch — used by the widget on all platforms.
    No auth required. Resolves per-tenant store client.
    Returns normalized cart regardless of platform (WooCommerce or Shopify).
    """
    if not store_client:
        return {"is_empty": True, "item_count": 0, "total": "0", "items": []}
    try:
        cart = await store_client.get_cart(session_id=session_id)
        return cart
    except Exception as exc:
        logger.warning("Cart fetch failed for session %s: %s", session_id, exc)
        return {"is_empty": True, "item_count": 0, "total": "0", "items": []}
