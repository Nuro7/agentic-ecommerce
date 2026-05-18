"""Public widget endpoints — no auth required."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ...agent.prompts.filtering import detect_language
from ...config import settings

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


@router.post("/greet")
async def greet_endpoint(payload: GreetRequest, req: Request):
    session_service = getattr(req.app.state, "session_service", None)
    store_client = getattr(req.app.state, "store_client", None)
    tts_service = getattr(req.app.state, "tts_service", None)

    session_id = payload.session_id
    store_name = payload.store_name or settings.store_name

    session_meta = await session_service.get_meta(session_id) if session_service else {}
    history = await session_service.get_history(session_id) if session_service else []

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

    audio_b64 = None
    if tts_service:
        try:
            save_meta_coro = (
                session_service.save_meta(session_id, {**session_meta, "greeted": True, "language": language})
                if session_service else asyncio.sleep(0)
            )
            audio_b64, _ = await asyncio.gather(
                tts_service.synthesize(greeting_text, language=language),
                save_meta_coro,
            )
        except Exception as exc:
            logger.warning("TTS synthesis failed: %s", exc)
    elif session_service:
        await session_service.save_meta(session_id, {**session_meta, "greeted": True, "language": language})

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


@router.get("/cart")
async def get_cart(session_id: str, req: Request):
    """
    Public cart fetch — used by the widget on all platforms.
    No auth required. Uses the store client from app.state.
    Returns normalized cart regardless of platform (WooCommerce or Shopify).
    """
    store_client = getattr(req.app.state, "store_client", None)
    if not store_client:
        return {"is_empty": True, "item_count": 0, "total": "0", "items": []}
    try:
        cart = await store_client.get_cart(session_id=session_id)
        return cart
    except Exception as exc:
        logger.warning("Cart fetch failed for session %s: %s", session_id, exc)
        return {"is_empty": True, "item_count": 0, "total": "0", "items": []}
