from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from agent.language import detect_language, make_speech_friendly
from agent.prompts import build_system_prompt
from services.beta_logger import get_beta_logger
from services.llm_clients import ANY_LLM_AVAILABLE
from services.llm_router import route_and_call
from services.security import sanitize_text
from services.session import SessionService
from services.session_facts import get_session_facts_service
from services.woocommerce import WooCommerceClient

logger = logging.getLogger(__name__)

_SUPPORTED_LANGS = {"en", "hi", "ml", "ta", "te", "bn", "kn", "gu", "pa"}


class AddressCollectionState:
    IDLE = "idle"
    COLLECTING_NAME = "collecting_name"
    COLLECTING_LAST_NAME = "collecting_last_name"
    COLLECTING_ADDRESS_LINE1 = "collecting_address_line1"
    COLLECTING_CITY = "collecting_city"
    COLLECTING_STATE = "collecting_state"
    COLLECTING_PINCODE = "collecting_pincode"
    COLLECTING_PHONE = "collecting_phone"
    COLLECTING_EMAIL = "collecting_email"
    CONFIRMING = "confirming"
    COMPLETE = "complete"


@dataclass
class AddressData:
    first_name: str = ""
    last_name: str = ""
    address_line1: str = ""
    city: str = ""
    state: str = ""
    postcode: str = ""
    phone: str = ""
    email: str = ""

    def is_complete(self) -> bool:
        # email is optional (user can say "skip"), so exclude from required check
        return all([self.first_name, self.last_name, self.address_line1, self.city, self.postcode, self.phone])

    def to_woocommerce_format(self) -> dict:
        return {
            "first_name": self.first_name,
            "last_name": self.last_name,
            "address_1": self.address_line1,
            "city": self.city,
            "state": self.state,
            "postcode": self.postcode,
            "country": os.getenv("STORE_COUNTRY", "IN"),
            "phone": self.phone,
            "email": self.email,
        }


class AgentOrchestrator:
    def __init__(
        self,
        woocommerce_service: WooCommerceClient,
        session_service: SessionService,
        tts_service=None,
    ) -> None:
        self.woo = woocommerce_service
        self.session = session_service
        self.tts = tts_service
        self._catalog_cache: str = ""
        self._catalog_cache_ts: float = 0.0

    async def run(
        self,
        session_id: str,
        user_message: str,
        store_context: Optional[Dict[str, Any]] = None,
        page_context: Optional[Dict[str, Any]] = None,
        language: str = "en",
        cart_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        store_context = store_context or {}
        page_context = page_context or {}
        cleaned_message = sanitize_text(user_message or "", max_len=500)
        if not cleaned_message:
            text = "Hi! I'm your shopping assistant. How can I help you today?"
            speech = make_speech_friendly(text, language)
            return {
                "session_id": session_id,
                "text": text,
                "response_text": text,
                "speech_text": speech,
                "language": self._normalize_language(language),
                "ui_actions": [],
                "actions": [],
                "suggested_replies": ["Show products", "Show my cart", "Help me find something"],
            }

        state = await self.session.get_session(session_id)
        history = state.get("conversation_history", []) if isinstance(state, dict) else []
        last_products = state.get("last_products", []) if isinstance(state, dict) else []

        detected_lang = detect_language(cleaned_message)
        session_meta = await self.session.get_meta(session_id)
        prev_lang = session_meta.get("language", "en")
        lang = detected_lang if detected_lang != "en" else prev_lang
        await self.session.save_meta(session_id, {**session_meta, "language": lang})

        # Prefer the cart context sent by the widget (browser's actual cart).
        # Fall back to a live fetch only when the widget didn't send one.
        if cart_context and isinstance(cart_context, dict) and cart_context.get("items"):
            cart = cart_context
        else:
            cart = await self._safe_get_cart(session_id)
        cart_for_prompt = {
            "is_empty": (int(cart.get("item_count") or cart.get("count") or 0) == 0),
            "item_count": int(cart.get("item_count") or cart.get("count") or 0),
            "total": str(cart.get("total") or "₹0"),
            "items": cart.get("items") or [],
        }

        result: Optional[Dict[str, Any]] = None
        lower_msg = cleaned_message.lower()

        # ── PRE-LLM: Simple deterministic intents (never need LLM) ───────────
        # Run these FIRST so the LLM router never needs to call an LLM
        # to refuse simple requests like "Store info" or "Show my cart".
        if (self._has_store_info_intent(lower_msg)
                or self._has_shipping_intent(lower_msg)
                or self._has_returns_intent(lower_msg)
                or self._has_payment_intent(lower_msg)
                or self._has_cart_view_intent(lower_msg)
                or self._has_remove_intent(lower_msg)):
            try:
                result = await self._run_fast_intent(
                    message=cleaned_message,
                    session_id=session_id,
                    language=lang,
                    store_context=store_context,
                )
            except Exception as fast_exc:
                logger.warning("Fast-intent pre-LLM failed: %s", fast_exc)

        # ── PRIMARY PATH: Full LLM agent ──────────────────────────────────────
        if result is None and ANY_LLM_AVAILABLE:
            try:
                result = await self._run_llm_agent(
                    session_id=session_id,
                    user_message=cleaned_message,
                    store_context=store_context,
                    page_context=page_context,
                    language=lang,
                    cart=cart_for_prompt,
                    history=history,
                    last_products=last_products,
                    cart_context=cart_context if cart_context else cart,
                )
            except Exception as exc:
                logger.warning("LLM agent failed (%s), falling back to deterministic handler.", exc)

        # ── FALLBACK PATH: Fast-intent for LLM failures ───────────────────────
        # Catches LLM rate limits/crashes so the widget always gets a useful response.
        if result is None:
            try:
                result = await self._run_fast_intent(
                    message=cleaned_message,
                    session_id=session_id,
                    language=lang,
                    store_context=store_context,
                )
            except Exception as fast_exc:
                logger.warning("Fast-intent also failed: %s", fast_exc)

        # ── FALLBACK PATH 2: Product browse as last-resort ───────────────────
        # Never show "Sorry, I missed that" — always try to show products.
        if result is None:
            try:
                products = await self.woo.search_products(query="", in_stock_only=True, limit=4)
                products = [p for p in (products or []) if isinstance(p, dict)]
                if products:
                    first = products[0]
                    name = first.get("name", "")
                    price = first.get("price") or first.get("regular_price") or ""
                    price_str = f"₹{price}" if price else ""
                    reply = f"Here's what I have for you — {name}{(', ' + price_str) if price_str else ''}. Want to know more, or looking for something specific?"
                    result = self._with_actions_alias({
                        "response_text": reply,
                        "ui_actions": [{"type": "show_products", "payload": {"products": [first]}}],
                        "suggested_replies": ["Tell me more", "Show my cart", "Search products"],
                    })
            except Exception:
                pass

        if result is None:
            result = {
                "response_text": "What are you looking for? I can help you find products, check your cart, or answer questions about the store.",
                "ui_actions": [],
                "suggested_replies": ["Show products", "Show my cart", "Store info"],
            }

        # ── SAFETY: ensure result is never None ─────────────────────────────
        if result is None:
            # Build a context-aware response rather than a generic one
            if last_products:
                product_name = last_products[0].get("name", "that product") if isinstance(last_products[0], dict) else "that product"
                fallback_text = f"I'm having a moment — did you want to add {product_name} to your cart?"
            else:
                fallback_text = "Sorry, I didn't catch that. What product are you looking for?"
            result = {
                "response_text": fallback_text,
                "ui_actions": [],
                "suggested_replies": ["Show products", "Show my cart", "Help me find something"],
            }

        # ── POST-PROCESSING ───────────────────────────────────────────────────
        response_text = str(result.get("response_text") or "I can help with products, cart, and checkout.").strip()
        ui_actions = result.get("ui_actions") if isinstance(result.get("ui_actions"), list) else result.get("actions", [])
        # Extract any NEXT: suggestions embedded by the LLM, then strip markup
        inline_suggestions, response_text = self._extract_next_suggestions(response_text)
        suggested = inline_suggestions or (result.get("suggested_replies") if isinstance(result.get("suggested_replies"), list) else [])
        response_text = self._strip_function_markup(response_text)
        if not response_text:
            response_text = self._summarize_actions_for_voice(ui_actions)
        if not response_text:
            response_text = "I'm here to help! Ask me about any product."

        # ── VOICE LENGTH ENFORCER ─────────────────────────────────────────────
        # Hard cap at 4 sentences — prevents the LLM from generating wall-of-text
        # responses that would take 20+ seconds to speak on a voice call.
        response_text = self._cap_to_sentences(response_text, max_sentences=4)

        speech_text = make_speech_friendly(response_text, lang)

        # Persist conversation history (keep last 30 turns = 15 back-and-forth)
        updated_history = (history if isinstance(history, list) else [])[-28:]
        updated_history.extend([
            {"role": "user", "content": cleaned_message},
            {"role": "assistant", "content": response_text},
        ])

        await self.session.update_session(
            session_id,
            conversation_history=updated_history,
            cart_snapshot=cart,
            customer_email=result.get("customer_email"),
            last_products=result.get("last_products") if isinstance(result.get("last_products"), list) else last_products,
        )

        # ── Post-turn: facts extraction + beta telemetry ──────────────────────
        try:
            tool_results_for_facts = [
                {"result": a.get("payload")} for a in ui_actions if isinstance(a, dict)
            ]
            await get_session_facts_service().update(
                session_id, cleaned_message, tool_results_for_facts
            )
        except Exception as _fe:
            logger.debug("SessionFacts update failed (non-critical): %s", _fe)

        try:
            _cart_value = float(
                str(cart.get("total") or "0").replace("₹", "").replace(",", "").strip() or "0"
            )
            _checkout = any(
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
                cart_value=_cart_value,
                checkout_reached=_checkout,
            )
        except Exception as _be:
            logger.debug("BetaLogger record failed (non-critical): %s", _be)

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

    async def handle_address_collection(
        self,
        session_id: str,
        user_message: str,
        current_state: str,
        address_data: dict,
        language: str,
    ) -> tuple[str, str, dict, list]:
        prompts = {
            "en": {
                "name": "What's your full name?",
                "last_name": "Please tell me your last name.",
                "address": "What's your delivery address?",
                "city": "Which city should we deliver to?",
                "state": "Which state?",
                "pincode": "What's your PIN code?",
                "phone": "Your phone number for delivery updates?",
                "email": "What email should we use for order updates?",
                "confirm": "Got it! Delivering to {name}, {address}, {city} {pincode}. Phone: {phone}. Email: {email}. Shall I proceed to payment?",
                "done": "Perfect! Taking you to payment now. Just complete the payment and you're done!",
            },
            "hi": {
                "name": "Aapka poora naam kya hai?",
                "last_name": "Aapka last name batayiye.",
                "address": "Delivery address kya hai?",
                "city": "Kaun se sheher mein deliver karein?",
                "state": "Kaun sa state?",
                "pincode": "PIN code kya hai?",
                "phone": "Delivery updates ke liye phone number?",
                "email": "Order updates ke liye email kya hai?",
                "confirm": "Theek hai! {name} ko {address}, {city} {pincode} pe deliver karenge. Phone: {phone}. Email: {email}. Kya payment pe jaayein?",
                "done": "Perfect! Ab payment ke liye ja rahe hain. Sirf payment complete karein!",
            },
            "ml": {
                "name": "Ningalude muthuperu enthanu?",
                "last_name": "Ningalude last name parayamo?",
                "address": "Delivery address?",
                "city": "Etu nagar/district?",
                "state": "State?",
                "pincode": "PIN code?",
                "phone": "Phone number?",
                "email": "Order updatesinu email enthaanu?",
                "confirm": "{name}, {address}, {city} {pincode} enthu sheriyano? Phone: {phone}. Email: {email}?",
                "done": "Sheriyanu! Payment cheyyan pokuva. Payment matram cheyyal mathi!",
            },
        }

        lang_prompts = prompts.get(language, prompts["en"])
        addr = AddressData()
        if isinstance(address_data, dict):
            for key, value in address_data.items():
                if hasattr(addr, key):
                    setattr(addr, key, str(value or "").strip())

        next_state = current_state
        response = ""
        ui_actions: List[Dict[str, Any]] = []
        cleaned = sanitize_text(user_message or "", max_len=250)

        if current_state == AddressCollectionState.COLLECTING_NAME:
            parts = cleaned.split(maxsplit=1)
            addr.first_name = parts[0] if parts else ""
            if len(parts) > 1:
                addr.last_name = parts[1]
                next_state = AddressCollectionState.COLLECTING_ADDRESS_LINE1
                response = lang_prompts["address"]
            else:
                next_state = AddressCollectionState.COLLECTING_LAST_NAME
                response = lang_prompts["last_name"]

        elif current_state == AddressCollectionState.COLLECTING_LAST_NAME:
            last_name = cleaned.strip()
            if last_name:
                addr.last_name = last_name
                next_state = AddressCollectionState.COLLECTING_ADDRESS_LINE1
                response = lang_prompts["address"]
            else:
                response = lang_prompts["last_name"]

        elif current_state == AddressCollectionState.COLLECTING_ADDRESS_LINE1:
            addr.address_line1 = cleaned
            next_state = AddressCollectionState.COLLECTING_CITY
            response = lang_prompts["city"]

        elif current_state == AddressCollectionState.COLLECTING_CITY:
            addr.city = cleaned
            next_state = AddressCollectionState.COLLECTING_STATE
            response = lang_prompts["state"]

        elif current_state == AddressCollectionState.COLLECTING_STATE:
            addr.state = self._normalize_india_state(cleaned)
            next_state = AddressCollectionState.COLLECTING_PINCODE
            response = lang_prompts["pincode"]

        elif current_state == AddressCollectionState.COLLECTING_PINCODE:
            numbers = re.findall(r"\d+", self._speech_digits_to_ascii(cleaned).replace(" ", ""))
            pincode = "".join(numbers)[:6]
            if len(pincode) == 6:
                addr.postcode = pincode
                next_state = AddressCollectionState.COLLECTING_PHONE
                response = lang_prompts["phone"]
            else:
                response = "I need a 6-digit PIN code. Could you repeat it?"

        elif current_state == AddressCollectionState.COLLECTING_PHONE:
            numbers = re.findall(r"\d+", self._speech_digits_to_ascii(cleaned).replace(" ", ""))
            phone = "".join(numbers)
            if len(phone) >= 10:
                addr.phone = phone[-10:]
                next_state = AddressCollectionState.COLLECTING_EMAIL
                response = lang_prompts["email"]
            else:
                response = "I need a 10-digit phone number. Could you say it again?"

        elif current_state == AddressCollectionState.COLLECTING_EMAIL:
            lowered = cleaned.lower()
            if "skip" in lowered or "no email" in lowered:
                addr.email = ""
                next_state = AddressCollectionState.CONFIRMING
                response = lang_prompts["confirm"].format(
                    name=f"{addr.first_name} {addr.last_name}".strip(),
                    address=addr.address_line1,
                    city=addr.city,
                    pincode=addr.postcode,
                    phone=addr.phone,
                    email=addr.email or "not provided",
                )
                ui_actions.append(
                    {
                        "type": "prefill_address",
                        "payload": addr.to_woocommerce_format(),
                    }
                )
            else:
                email = self._extract_email(lowered)
                if email:
                    addr.email = email
                    next_state = AddressCollectionState.CONFIRMING
                    response = lang_prompts["confirm"].format(
                        name=f"{addr.first_name} {addr.last_name}".strip(),
                        address=addr.address_line1,
                        city=addr.city,
                        pincode=addr.postcode,
                        phone=addr.phone,
                        email=addr.email,
                    )
                    ui_actions.append(
                        {
                            "type": "prefill_address",
                            "payload": addr.to_woocommerce_format(),
                        }
                    )
                else:
                    response = "Please tell a valid email address, or say skip."

        elif current_state == AddressCollectionState.CONFIRMING:
            affirmative = {
                # English
                "yes", "yeah", "yep", "yup", "ok", "okay", "sure", "correct",
                "right", "of course", "certainly", "absolutely", "definitely",
                "go ahead", "go", "proceed", "confirm", "confirmed", "done",
                "perfect", "alright", "fine", "great", "sounds good", "do it",
                "let's go", "lets go", "place order", "pay now",
                # Hindi
                "haan", "ha", "acha", "theek", "bilkul", "zaroor", "karo",
                # Malayalam
                "seri", "aayi", "sheriyanu", "sheriya", "ittekkaamo",
                # Tamil
                "sari", "aamam", "seyyungal",
                # Telugu
                "avunu", "sare", "cheyyi",
            }
            lowered = cleaned.lower()
            if any(token in lowered for token in affirmative):
                next_state = AddressCollectionState.COMPLETE
                response = lang_prompts["done"]
                ui_actions.append(
                    {
                        "type": "redirect_checkout_with_address",
                        "payload": {
                            "url": "/checkout",
                            "billing": addr.to_woocommerce_format(),
                            "shipping": addr.to_woocommerce_format(),
                        },
                    }
                )
            else:
                next_state = AddressCollectionState.COLLECTING_NAME
                response = "No problem, let's start over. " + lang_prompts["name"]

        return response, next_state, addr.__dict__, ui_actions

    @staticmethod
    def _speech_digits_to_ascii(text: str) -> str:
        value = str(text or "").lower()
        digit_words = {
            "zero": "0",
            "one": "1",
            "two": "2",
            "three": "3",
            "four": "4",
            "five": "5",
            "six": "6",
            "seven": "7",
            "eight": "8",
            "nine": "9",
        }
        for word, digit in digit_words.items():
            value = re.sub(rf"\b{word}\b", digit, value)
        value = value.translate(str.maketrans("०१२३४५६७८९", "0123456789"))
        return value

    @staticmethod
    def _normalize_india_state(text: str) -> str:
        raw = str(text or "").strip()
        if not raw:
            return ""
        normalized = re.sub(r"\s+", " ", raw).lower().strip()
        mapping = {
            "andhra pradesh": "AP",
            "arunachal pradesh": "AR",
            "assam": "AS",
            "bihar": "BR",
            "chhattisgarh": "CG",
            "goa": "GA",
            "gujarat": "GJ",
            "haryana": "HR",
            "himachal pradesh": "HP",
            "jharkhand": "JH",
            "karnataka": "KA",
            "kerala": "KL",
            "madhya pradesh": "MP",
            "maharashtra": "MH",
            "manipur": "MN",
            "meghalaya": "ML",
            "mizoram": "MZ",
            "nagaland": "NL",
            "odisha": "OR",
            "orissa": "OR",
            "punjab": "PB",
            "rajasthan": "RJ",
            "sikkim": "SK",
            "tamil nadu": "TN",
            "telangana": "TS",
            "tripura": "TR",
            "uttar pradesh": "UP",
            "uttarakhand": "UK",
            "west bengal": "WB",
            "delhi": "DL",
            "jammu and kashmir": "JK",
            "ladakh": "LA",
            "puducherry": "PY",
        }
        if normalized in mapping:
            return mapping[normalized]
        if re.fullmatch(r"[a-zA-Z]{2}", raw):
            return raw.upper()
        return raw

    def _should_use_llm(self, message: str) -> bool:
        """
        Returns True if message MUST go to LLM (not fast intent).
        Pronouns and references require conversation context
        that only the LLM has access to.
        """
        message_lower = message.lower()
        
        # Context-dependent pronouns — LLM must resolve these
        # Only single-word pronouns that are unambiguously referential
        context_words = {
            # English — standalone pronouns meaning "that/previously shown thing"
            'it', 'that', 'this', 'those', 'these', 'ones',
            # Note: 'one' excluded — too common ("add one", "show one product")
            # Note: 'first'/'second'/'last' excluded alone — covered by phrases below
            # Hindi
            'woh', 'yeh', 'iska', 'uska', 'pehla', 'doosra',
            # Malayalam
            'athu', 'ithu', 'avar', 'ivan',
            # Tamil
            'avan',
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

    async def _run_fast_intent(
        self,
        message: str,
        session_id: str,
        state: Optional[Dict[str, Any]] = None,
        language: str = "en",
        store_context: Optional[Dict[str, Any]] = None,
        force: bool = False,
    ) -> Optional[Dict[str, Any]]:
        text = sanitize_text(message or "", max_len=500)
        lower = text.lower()
        store_name = str((store_context or {}).get("store_name") or "").strip()

        # Only handle the three deterministic intents that don't benefit from LLM reasoning.
        # Everything else (buy, add, compare, order, inventory, product discovery, checkout)
        # must go through the LLM so responses are natural and context-aware.

        # Specific policy questions — answer only that field, no full store card
        if self._has_shipping_intent(lower):
            _shipping = os.getenv("STORE_SHIPPING_POLICY", "")
            if _shipping:
                return self._with_actions_alias({
                    "response_text": _shipping,
                    "suggested_replies": ["Show products", "Return policy", "Payment methods"],
                })

        if self._has_returns_intent(lower):
            _returns = os.getenv("STORE_RETURNS_POLICY", "")
            if _returns:
                return self._with_actions_alias({
                    "response_text": _returns,
                    "suggested_replies": ["Show products", "Delivery charges", "Payment methods"],
                })

        if self._has_payment_intent(lower):
            _payments = os.getenv("STORE_PAYMENT_METHODS", "")
            if _payments:
                return self._with_actions_alias({
                    "response_text": f"We accept: {_payments}.",
                    "suggested_replies": ["Show products", "Delivery charges", "Return policy"],
                })

        if self._has_store_info_intent(lower):
            # Build rich store info from env vars
            _sname = store_name or os.getenv("STORE_NAME", "this store")
            _about    = os.getenv("STORE_ABOUT", "")
            _shipping = os.getenv("STORE_SHIPPING_POLICY", "")
            _returns  = os.getenv("STORE_RETURNS_POLICY", "")
            _payments = os.getenv("STORE_PAYMENT_METHODS", "")
            _currency = os.getenv("STORE_CURRENCY", "₹")
            # Build spoken reply
            parts = [f"Welcome to {_sname}!"]
            if _about:
                parts.append(_about)
            if _shipping:
                parts.append(_shipping)
            if _returns:
                parts.append(_returns)
            if _payments:
                parts.append(f"We accept: {_payments}.")
            store_reply = " ".join(parts)
            # Build UI payload for the store info card
            store_info_payload = {
                "store_name": _sname,
                "about": _about,
                "currency": _currency,
                "shipping": _shipping,
                "returns": _returns,
                "payment_methods": _payments,
            }
            return self._with_actions_alias({
                "response_text": store_reply,
                "ui_actions": [{"type": "show_store_info", "payload": store_info_payload}],
                "suggested_replies": ["Show products", "Show my cart", "Browse"],
            })

        if self._has_cart_view_intent(lower):
            cart = await self._safe_get_cart(session_id)
            return self._with_actions_alias({
                "response_text": self._say(language, "cart_opened"),
                "ui_actions": [{"type": "show_cart", "payload": {"cart": self._normalize_cart_payload(cart)}}],
                "suggested_replies": ["Checkout now", "Show products"],
            })

        if self._has_remove_intent(lower):
            cart = await self._safe_get_cart(session_id)
            items = cart.get("items") if isinstance(cart.get("items"), list) else []
            if not items:
                return self._with_actions_alias({
                    "response_text": self._say(language, "cart_empty"),
                    "ui_actions": [{"type": "show_cart", "payload": {"cart": self._normalize_cart_payload(cart)}}],
                    "suggested_replies": ["Show products"],
                })
            target = items[-1]
            try:
                await self.woo.remove_from_cart(session_id=session_id, cart_item_key=target.get("cart_item_key"))
            except Exception:
                pass
            cart_after = await self._safe_get_cart(session_id)
            return self._with_actions_alias({
                "response_text": self._say(language, "removed_from_cart", name=target.get("name", "item")),
                "ui_actions": [{"type": "show_cart", "payload": {"cart": self._normalize_cart_payload(cart_after)}}],
                "suggested_replies": ["Checkout now", "Show products"],
            })

        # ── Browse / Show products / Best sellers fallback ───────────────────
        # Only runs when called as LLM fallback (LLM already failed).
        # If the LLM is working, it handles these naturally — this is a safety net.
        browse_tokens = [
            "show products", "show best", "best sellers", "bestsellers",
            "browse", "what do you have", "what products", "show me products",
            "show items", "what's available", "what is available",
            "show all", "products", "items available",
            # natural browsing phrases not caught by the above
            "what are the available", "available product", "available items",
            "what have you got", "what you have", "what do you sell",
            "what can i buy", "see all", "see products", "list products",
        ]
        if any(token in lower for token in browse_tokens) or lower.strip() in ("browse", "products", "shop"):
            try:
                products = await self.woo.search_products(query="", in_stock_only=True, limit=6)
                products = [p for p in (products or []) if isinstance(p, dict)]
                if products:
                    first = products[0]
                    name = first.get("name", "")
                    price = first.get("price") or first.get("regular_price") or ""
                    price_str = f"₹{price}" if price else ""
                    reply = f"{name}{(', ' + price_str) if price_str else ''}. Want me to tell you more, or check size options?"
                    # Show only the ONE product the agent is talking about
                    return self._with_actions_alias({
                        "response_text": reply,
                        "ui_actions": [{"type": "show_products", "payload": {"products": [first]}}],
                        "suggested_replies": ["Tell me more", "Add to cart", "Show my cart"],
                    })
            except Exception:
                pass

        # ── Generic product search fallback ──────────────────────────────────
        # Only runs as LLM fallback. Try to search with the user's raw message.
        # If the LLM is working, it handles this with full context. This is a safety net.
        try:
            query = self._normalize_discovery_query(text)
            if query.strip():
                products = await self.woo.search_products(query=query, in_stock_only=False, limit=5)
                products = [p for p in (products or []) if isinstance(p, dict)]
                if products:
                    first = products[0]
                    name = first.get("name", "")
                    price = first.get("price") or first.get("regular_price") or ""
                    price_str = f"₹{price}" if price else ""
                    reply = f"{name}{(', ' + price_str) if price_str else ''}. Want me to tell you more, or shall I check size options?"
                    # Show only the ONE product the agent is talking about
                    return self._with_actions_alias({
                        "response_text": reply,
                        "ui_actions": [{"type": "show_products", "payload": {"products": [first]}}],
                        "suggested_replies": ["Tell me more", "Add to cart", "Show my cart"],
                    })
                else:
                    # Nothing found — show what we do have
                    all_products = await self.woo.search_products(query="", in_stock_only=True, limit=4)
                    all_products = [p for p in (all_products or []) if isinstance(p, dict)]
                    if all_products:
                        names = ", ".join(p.get("name", "") for p in all_products[:3] if p.get("name"))
                        reply = f"I couldn't find that exactly, but we have {names} and more. Want me to show you?"
                        return self._with_actions_alias({
                            "response_text": reply,
                            "ui_actions": [{"type": "show_products", "payload": {"products": all_products}}],
                            "suggested_replies": ["Show products", "Show my cart"],
                        })
        except Exception:
            pass

        # Everything else — let the LLM handle it naturally
        return None

    async def _run_llm_agent(
        self,
        *,
        session_id: str,
        user_message: str,
        store_context: Dict[str, Any],
        page_context: Dict[str, Any],
        language: str,
        cart: Dict[str, Any],
        history: List[Dict[str, Any]],
        last_products: Optional[List[Any]] = None,
        cart_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if not ANY_LLM_AVAILABLE:
            return None

        # Fetch store catalog — cached for 5 minutes
        store_catalog = ""
        if self._catalog_cache and (time.time() - self._catalog_cache_ts) < 300:
            store_catalog = self._catalog_cache
        else:
            try:
                catalog_parts = []

                # 1. Product categories
                categories = await self.woo.get_categories()
                if categories:
                    cat_names = [c.get("name", "") for c in categories if isinstance(c, dict) and c.get("name") and c.get("count", 1) > 0]
                    if cat_names:
                        catalog_parts.append("Categories: " + ", ".join(cat_names))

                # 2. Sample in-stock products (for agent awareness)
                sample = await self.woo.search_products(query="", in_stock_only=True, limit=10)
                if sample:
                    names = list({p.get("name", "").split(" – ")[0].strip() for p in sample if isinstance(p, dict) and p.get("name")})[:8]
                    if names:
                        catalog_parts.append("Available products include: " + ", ".join(names))

                # 3. On-sale products (so agent can proactively mention deals)
                try:
                    sale_items = await self.woo.search_products(query="", in_stock_only=True, limit=5)
                    on_sale = [p for p in sale_items if isinstance(p, dict) and p.get("sale_price") and p.get("regular_price") and p.get("sale_price") != p.get("regular_price")]
                    if on_sale:
                        sale_names = [f"{p.get('name')} (was ₹{p.get('regular_price')}, now ₹{p.get('sale_price')})" for p in on_sale[:3]]
                        catalog_parts.append("Current deals: " + ", ".join(sale_names))
                except Exception:
                    pass

                store_catalog = "\n".join(catalog_parts)
                if store_catalog:
                    self._catalog_cache = store_catalog
                    self._catalog_cache_ts = time.time()
            except Exception as cat_err:
                logger.debug("Could not pre-fetch store catalog: %s", cat_err)

        system_prompt = build_system_prompt(
            store_context=store_context,
            cart=cart,
            page_context=page_context,
            language=language,
            address_state=AddressCollectionState.IDLE,
            store_catalog=store_catalog,
        )

        # Inject session facts (customer size/color/budget preferences)
        try:
            facts = await get_session_facts_service().get(session_id)
            facts_line = get_session_facts_service().format_for_prompt(facts)
            if facts_line:
                system_prompt += "\n\n" + facts_line
        except Exception as _fe:
            logger.debug("SessionFacts get failed (non-critical): %s", _fe)

        tools = self._tool_schema()
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "system", "content": (
                "VOICE CALL RULES — FOLLOW EXACTLY:\n"
                "1. After tool results: pick ONE product, speak 2-3 sentences about it, ask one question. Done.\n"
                "2. Never output JSON, markdown, bullet points, numbered lists, or asterisks.\n"
                "3. Never say 'Based on the search results', 'According to the data', 'I found X matches', 'I see that', 'I have found', 'I searched'.\n"
                "4. Never describe more than 1 product per response unless the customer explicitly asked to compare.\n"
                "5. Max 3 sentences total. If you want to say more — stop. They'll ask.\n"
                "6. Sound like a person, not a search engine. Talk about the product like you know it."
            )},
        ]

        # Inject recently viewed products as context so agent can resolve "that one", "tell me more", etc.
        if last_products:
            compact = [{"id": p.get("id"), "name": p.get("name"), "price": p.get("price")} for p in last_products[:5] if isinstance(p, dict)]
            if compact:
                messages.append({
                    "role": "system",
                    "content": (
                        f"Recently shown products with their IDs: {json.dumps(compact, ensure_ascii=False)}\n"
                        "If the customer asks for more info, call get_product_details(id) with the exact ID above. "
                        "Do NOT say you can't fetch — just call the tool."
                    )
                })

        for entry in (history or [])[-20:]:  # keep last 20 turns (10 back-and-forth)
            if not isinstance(entry, dict):
                continue
            role = str(entry.get("role", "")).strip().lower()
            content = str(entry.get("content", "")).strip()
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})

        messages.append({"role": "user", "content": user_message})

        actions: List[Dict[str, Any]] = []
        last_products: List[Any] = []
        customer_email: Optional[str] = None
        last_llm_route = "gpt-4o-mini"
        tool_rounds_done = 0   # how many tool-call rounds have completed

        for _ in range(5):  # 5 rounds: search → details → variants/inventory → response
            # Allow 3 tool rounds before forcing spoken text.
            # 2 rounds is too tight: "is X in size M available?" needs search → check_inventory → find_variants (3 rounds).
            # 4+ rounds risk silent loops; 3 gives: search → check/add → variants/resolve → final text.
            force_text = tool_rounds_done >= 3
            llm_resp = await route_and_call(
                messages=messages,
                tools=tools,
                lang=language,
                address_active=False,
                turn_count=len(history),
                message_text=user_message,
                force_text=force_text,
            )
            if not llm_resp:
                break
            last_llm_route = llm_resp.get("llm_route", "gpt-4o-mini")
            raw_content = llm_resp.get("text") or ""
            tool_calls: List[Dict[str, Any]] = llm_resp.get("tool_calls") or []

            if not tool_calls:
                # Strip any residual reasoning blocks
                raw_content = re.sub(r"<think>.*?</think>", "", raw_content, flags=re.DOTALL).strip()
                inline_calls, cleaned_content = self._extract_inline_function_calls(raw_content)

                if inline_calls:
                    for tool_name, tool_args in inline_calls:
                        tool_result, tool_actions, product_ids, maybe_email = await self._execute_tool_call(
                            tool_name=tool_name,
                            tool_args=tool_args,
                            session_id=session_id,
                            cart_context=cart_context,
                        )
                        if tool_actions:
                            actions.extend(tool_actions)
                        if product_ids:
                            for pid in product_ids:
                                if pid and pid not in last_products:
                                    last_products.append(pid)
                        if maybe_email:
                            customer_email = maybe_email
                        messages.append({"role": "assistant", "content": f"Executed inline function {tool_name}"})
                        messages.append({"role": "assistant", "content": json.dumps(tool_result, ensure_ascii=False)})

                fallback_text = self._summarize_actions_for_voice(actions)
                raw_final = cleaned_content or fallback_text or ""
                llm_replies, raw_final = self._extract_next_suggestions(raw_final)
                final = sanitize_text(raw_final, max_len=2000)
                if not final:
                    final = self._summarize_actions_for_voice(actions)

                if not final:
                    return None

                return {
                    "response_text": final,
                    "ui_actions": actions,
                    "suggested_replies": llm_replies,
                    "last_products": last_products,
                    "customer_email": customer_email,
                    "llm_route": last_llm_route,
                }

            # Append assistant turn with tool call references (OpenAI format for history)
            messages.append(
                {
                    "role": "assistant",
                    "content": raw_content,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
                            },
                        }
                        for tc in tool_calls
                    ],
                }
            )

            for tc in tool_calls:
                tool_name = tc["name"]
                tool_args = tc["arguments"]   # already a dict from router

                try:
                    tool_result, tool_actions, product_ids, maybe_email = await self._execute_tool_call(
                        tool_name=tool_name,
                        tool_args=tool_args,
                        session_id=session_id,
                        cart_context=cart_context,
                    )
                except Exception as tool_exc:
                    logger.warning("Tool %s failed: %s", tool_name, tool_exc)
                    tool_result = {"error": f"Tool {tool_name} temporarily unavailable. Please try again."}
                    tool_actions, product_ids, maybe_email = [], [], None

                if tool_actions:
                    actions.extend(tool_actions)
                if product_ids:
                    for pid in product_ids:
                        if pid and pid not in last_products:
                            last_products.append(pid)
                if maybe_email:
                    customer_email = maybe_email

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": tool_name,
                        "content": json.dumps(tool_result, ensure_ascii=False),
                    }
                )

            tool_rounds_done += 1   # one full tool-execution round complete

        # Loop exhausted (5 tool-call rounds) — return whatever was accumulated
        if actions:
            fallback_text = self._summarize_actions_for_voice(actions)
            return {
                "response_text": fallback_text or "Done! What else can I help you with?",
                "ui_actions": actions,
                "suggested_replies": [],
                "last_products": last_products,
                "customer_email": customer_email,
                "llm_route": last_llm_route,
            }
        return None

    async def _execute_tool_call(
        self,
        *,
        tool_name: str,
        tool_args: Dict[str, Any],
        session_id: str,
        cart_context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Any], Optional[str]]:
        actions: List[Dict[str, Any]] = []
        product_ids: List[Any] = []
        customer_email: Optional[str] = None

        if tool_name == "search_products":
            raw_query = str(tool_args.get("query", "")).strip()
            brand = str(tool_args.get("brand", "") or "").strip()
            # If a brand is specified, fold it into the query so full-text search picks it up
            if brand and brand.lower() not in raw_query.lower():
                raw_query = f"{brand} {raw_query}".strip()
            query = self._normalize_discovery_query(raw_query)
            # For specific queries keep results tight (5) so LLM can reason over them.
            # For browse/no-query use 6 to show a useful grid without overwhelming the UI.
            default_limit = 6 if not query else 5
            requested_limit = self._safe_int(tool_args.get("limit"), default_limit)
            limit = max(1, min(requested_limit, 8))  # hard cap at 8
            # Default in_stock_only to False so availability queries never accidentally
            # filter out products — the LLM reads stock_status from results and responds accurately.
            in_stock_only = bool(tool_args.get("in_stock_only", False))
            products = await self.woo.search_products(
                query=query,
                category_slug=tool_args.get("category"),
                min_price=self._safe_float(tool_args.get("min_price")),
                max_price=self._safe_float(tool_args.get("max_price")),
                in_stock_only=in_stock_only,
                limit=limit,
            )
            # If brand was specified and we got results, filter to those that actually mention the brand.
            # If none match, keep all results so the LLM can offer alternatives.
            brand_filtered: List[Dict[str, Any]] = []
            if brand and products:
                bl = brand.lower()
                brand_filtered = [
                    p for p in products
                    if bl in str(p.get("name") or "").lower()
                    or bl in str(p.get("short_description") or p.get("description") or "").lower()
                ]
                # brand_found tells the LLM whether the brand was actually found
                brand_found = len(brand_filtered) > 0
            else:
                brand_found = True  # no brand filter, irrelevant
            if not products and raw_query and query != "":
                products = await self.woo.search_products(
                    query="",
                    category_slug=tool_args.get("category"),
                    min_price=self._safe_float(tool_args.get("min_price")),
                    max_price=self._safe_float(tool_args.get("max_price")),
                    in_stock_only=False,
                    limit=min(limit, 8),
                )
                brand_found = False
            # Smart re-ranking and filtering:
            # - Specific product query  → show only that product (exact name match)
            # - Category/browse query   → show all relevant products
            # - Zero-relevance products → always removed
            if query and products:
                query_words = [w for w in query.lower().split() if len(w) > 2]

                def _relevance(p: dict) -> int:
                    name_lower = str(p.get("name") or "").lower()
                    desc_lower = str(p.get("short_description") or p.get("description") or "").lower()
                    name_score = sum(2 for w in query_words if w in name_lower)
                    desc_score = sum(1 for w in query_words if w in desc_lower)
                    return name_score + desc_score

                products_sorted = sorted(products, key=_relevance, reverse=True)
                best_score = _relevance(products_sorted[0]) if products_sorted else 0

                if best_score > 0:
                    relevant = [p for p in products_sorted if _relevance(p) > 0]

                    # Specific search: 2+ query words AND at least one product has ALL
                    # of them in its name → show only exact-name-match products
                    if len(query_words) >= 2:
                        exact = [
                            p for p in relevant
                            if all(w in str(p.get("name") or "").lower() for w in query_words)
                        ]
                        if exact:
                            products = exact  # only the specifically named product(s)
                        else:
                            products = relevant
                    else:
                        # Single-word / category query — show all relevant, no minimum
                        products = relevant

            actions.append({"type": "show_products", "payload": {"products": products}})
            product_ids = [p.get("id") for p in products if p.get("id")]
            if products:
                await self.session.save_meta(session_id, {'last_products': products[:8]})
            # Give LLM a compact summary so it can identify the correct product
            compact = [
                {"id": p.get("id"), "name": p.get("name"), "price": p.get("price"), "in_stock": p.get("in_stock")}
                for p in products
            ]
            result: Dict[str, Any] = {"products": compact, "count": len(products)}
            if brand:
                result["brand_searched"] = brand
                result["brand_found"] = brand_found
                if not brand_found:
                    result["note"] = f"Brand '{brand}' not found in catalog. Showing similar alternatives — tell the customer we don't carry that brand but suggest the best alternative."
            return result, actions, product_ids, None

        if tool_name == "get_product_details":
            raw_pid = tool_args.get("product_id")
            product_id = self._safe_int(raw_pid, 0)
            # LLM sometimes passes product name instead of ID — resolve it
            if not product_id and raw_pid and isinstance(raw_pid, str):
                logger.info("get_product_details: resolving name '%s' to ID via search", raw_pid)
                matches = await self.woo.search_products(query=raw_pid, in_stock_only=False, limit=1)
                if matches:
                    product_id = int(matches[0].get("id") or 0)
                    logger.info("Resolved product name '%s' → id=%d", raw_pid, product_id)
            product = await self.woo.get_product_details(product_id)
            if product.get("id"):
                product_ids.append(product.get("id"))
            actions.append({"type": "show_product_detail", "payload": {"product": product}})
            if product.get('id'):
                existing = await self.session.get_meta(session_id)
                last = existing.get('last_products', [])
                last = [product] + [p for p in last if (p.get('id') if isinstance(p, dict) else p) != product['id']]
                await self.session.save_meta(session_id, {'last_products': last[:8]})
            return {"product": product}, actions, product_ids, None

        if tool_name == "check_inventory":
            product_id = self._safe_int(tool_args.get("product_id"), 0)
            inventory = await self.woo.check_inventory(
                product_id=product_id,
                variation_id=self._safe_optional_int(tool_args.get("variation_id")),
                attributes=tool_args.get("attributes"),
            )
            details = await self.woo.get_product_details(product_id)
            product_name = details.get("name", "That product")
            actions.append(
                {
                    "type": "show_availability",
                    "payload": {
                        "product": {
                            "id": details.get("id"),
                            "name": product_name,
                            "price": details.get("price"),
                            "image_url": details.get("image_url", ""),
                            "stock_status": details.get("stock_status"),
                        },
                        "inventory": inventory,
                        "attributes": tool_args.get("attributes", {}),
                    },
                }
            )
            # Build a clear instruction so the LLM generates a useful spoken response
            if inventory.get("variant_not_found"):
                hint = (
                    f"The exact variant is not available for '{product_name}'. "
                    f"Call find_variants(product_id={product_id}) to show the customer available options."
                )
            elif inventory.get("in_stock"):
                qty = inventory.get("stock_quantity")
                qty_str = f" — {qty} units in stock" if isinstance(qty, int) and qty > 0 else ""
                hint = (
                    f"'{product_name}' IS IN STOCK{qty_str}. "
                    "Tell the customer it's available and ask if they'd like to add it to cart."
                )
            else:
                hint = (
                    f"'{product_name}' is OUT OF STOCK. "
                    "Apologize briefly and offer to show similar in-stock alternatives."
                )
            return {"inventory": inventory, "response_hint": hint}, actions, [product_id], None

        if tool_name == "get_cart":
            # Avoid backend request without cookies. Use the cart_context provided by the frontend.
            safe_cart = cart_context if isinstance(cart_context, dict) else {}
            actions.append({"type": "show_cart", "payload": {"cart": self._normalize_cart_payload(safe_cart)}})
            return {"cart": safe_cart}, actions, [], None

        if tool_name == "add_to_cart":
            product_id = self._safe_int(tool_args.get("product_id"), 0)
            variation_id = self._safe_int(tool_args.get("variation_id"), 0)
            quantity = max(1, min(self._safe_int(tool_args.get("quantity"), 1), 20))
            variation_data: Dict[str, Any] = {}

            if not variation_id and tool_args.get("attributes"):
                inv = await self.woo.check_inventory(
                    product_id=product_id,
                    attributes=tool_args.get("attributes"),
                )
                variation_id = self._safe_int(inv.get("variation_id"), 0)
                variation_data = self.woo._attributes_to_variation_map(inv.get("attributes", []))

            actions.append(
                {
                    "type": "add_to_cart",
                    "payload": {
                        "product_id": product_id,
                        "variation_id": variation_id,
                        "variation": variation_data,
                        "quantity": quantity,
                    },
                }
            )
            return {"add_to_cart": "client_side_action"}, actions, [product_id], None

        if tool_name == "remove_from_cart":
            cart_item_key = str(tool_args.get("cart_item_key") or "").strip()
            actions.append(
                {
                    "type": "remove_from_cart",
                    "payload": {
                        "cart_item_key": cart_item_key,
                    },
                }
            )
            return {"remove_from_cart": "client_side_action"}, actions, [], None

        if tool_name == "get_orders":
            email = str(tool_args.get("customer_email") or "").strip().lower()
            orders = await self.woo.get_orders(customer_email=email, limit=5)
            if orders:
                actions.append({"type": "show_orders", "payload": {"orders": orders}})
            customer_email = email if email else None
            return {"orders": orders}, actions, [], customer_email

        if tool_name == "apply_coupon":
            code = str(tool_args.get("coupon_code") or "").strip()
            result = await self.woo.apply_coupon(session_id=session_id, coupon_code=code)
            actions.append({"type": "coupon_applied", "payload": {"code": code, "discount": result.get("message", "Applied")}})
            return {"coupon": result}, actions, [], None

        if tool_name == "get_categories":
            try:
                categories = await self.woo.get_categories()
            except Exception as cat_err:
                logger.warning("get_categories failed (%s), falling back to product search", cat_err)
                categories = []
            if categories:
                # Format category list so LLM can read and describe them
                cat_names = [str(c.get("name", "")) for c in categories if c.get("name")]
                return {"categories": categories, "category_names": cat_names}, actions, [], None
            # Fallback: return product names as text context. Show ONLY the first product
            # as a card so the UI isn't flooded while still giving a visual anchor.
            products = await self.woo.search_products(query="", in_stock_only=True, limit=12)
            products = [p for p in (products or []) if isinstance(p, dict)]
            product_ids = [p.get("id") for p in products if p.get("id")]
            product_names = [p.get("name", "") for p in products if p.get("name")][:8]
            if products:
                actions.append({"type": "show_products", "payload": {"products": [products[0]]}})
            return {
                "categories": [],
                "note": "Category listing unavailable. The first product card is already shown. Recommend ONE product from this list by name, then ask if the customer wants to see more options.",
                "available_products": product_names,
                "count": len(products),
            }, actions, product_ids, None

        if tool_name == "update_cart_quantity":
            pid = self._safe_int(tool_args.get("product_id"), 0)
            qty = self._safe_int(tool_args.get("quantity"), 0)
            result = await self.woo.update_cart_quantity(session_id=session_id, product_id=pid, quantity=qty)
            actions.append({"type": "cart_updated", "payload": result})
            return {"update_cart_quantity": result}, actions, [], None

        if tool_name == "find_variants":
            pid = self._safe_int(tool_args.get("product_id"), 0)
            result = await self.woo.find_variants(product_id=pid)
            detail = await self.woo.get_product_details(pid)
            # Prefer API variations; fall back to variations_summary from product details
            variations = result.get("variations") or []
            if not variations and detail.get("variations_summary"):
                variations = detail["variations_summary"]
            payload = {"product": detail, "variations": variations}
            actions.append({"type": "show_variants", "payload": payload})
            var_count = len(variations)
            # Give the LLM a clear instruction so it generates "pick your options" text
            # instead of immediately calling add_to_cart with no variant selected.
            tool_result_msg = (
                f"Variant selector shown to user ({var_count} options for '{detail.get('name', '')}')."
                " IMPORTANT: Do NOT call add_to_cart now. Tell the user to select size/color/quantity"
                " from the options shown above, then tap Add to Cart."
            ) if var_count > 0 else (
                f"No variants found for '{detail.get('name', '')}'. Ask the user which option they need."
            )
            return {
                "find_variants": {
                    "product_name": detail.get("name", ""),
                    "variations_count": var_count,
                    "message": tool_result_msg,
                }
            }, actions, [pid], None

        if tool_name == "get_best_coupon":
            result = await self.woo.get_best_coupon()
            if result.get("code"):
                # Normalise key: woocommerce.py returns 'type', expose as 'discount_type' for consistency
                discount_type = result.get("discount_type") or result.get("type")
                # Only show toast/UI when a coupon actually exists
                actions.append({"type": "show_best_coupon", "payload": result})
                return {"coupon_available": True, "code": result["code"], "amount": result.get("amount"), "discount_type": discount_type, "display": result.get("display", "")}, actions, [], None
            # No coupon — return a clean result so LLM doesn't think there's a bug
            return {"coupon_available": False, "message": "No active coupons in this store right now."}, actions, [], None

        if tool_name == "submit_review":
            pid = self._safe_int(tool_args.get("product_id"), 0)
            rating = self._safe_int(tool_args.get("rating"), 5)
            text = str(tool_args.get("review") or "")
            name = str(tool_args.get("name") or "")
            result = await self.woo.submit_review(product_id=pid, rating=rating, review=text, name=name)
            actions.append({"type": "review_submitted", "payload": result})
            return {"submit_review": result}, actions, [pid], None

        if tool_name == "get_store_info":
            info = await self.woo.get_store_policies()
            return {"store_info": info}, actions, [], None

        if tool_name == "compare_products":
            compare_items: List[Dict[str, Any]] = []
            # Accept either integer IDs or string names
            raw_ids = tool_args.get("product_ids") or []
            raw_names = [tool_args.get("product_a"), tool_args.get("product_b")]
            # Build list of (id_or_none, name_or_none) to fetch
            to_fetch: List[Any] = []
            if raw_ids and isinstance(raw_ids, list):
                to_fetch = [{"id": int(x)} for x in raw_ids if x]
            else:
                to_fetch = [{"name": str(n).strip()} for n in raw_names if n]
            for item in to_fetch[:3]:
                row = None
                if item.get("id"):
                    row = await self.woo.get_product_details(int(item["id"]))
                elif item.get("name"):
                    rows = await self.woo.search_products(query=item["name"], in_stock_only=False, limit=1)
                    row = rows[0] if rows else None
                if row and row.get("id"):
                    details = await self.woo.get_product_details(int(row.get("id") or row.get("product_id") or 0)) if not row.get("variations") else row
                    compare_items.append({
                        "id": details.get("id") or row.get("id"),
                        "name": details.get("name") or row.get("name"),
                        "price": details.get("price") or row.get("price"),
                        "sale_price": details.get("sale_price") or row.get("sale_price"),
                        "in_stock": self._in_stock(details or row),
                        "image_url": (details or row).get("image_url") or "",
                        "permalink": (details or row).get("permalink", ""),
                        "short_description": (details or row).get("short_description", ""),
                        "rating": (details or row).get("average_rating") or (details or row).get("rating_count"),
                    })
            if len(compare_items) >= 2:
                actions.append({"type": "show_comparison", "payload": {"items": compare_items}})
            return {"comparison": compare_items, "count": len(compare_items)}, actions, [i.get("id") for i in compare_items if i.get("id")], None

        if tool_name == "get_reviews":
            product_id = self._safe_int(tool_args.get("product_id"), 0)
            if not product_id:
                return {"error": "product_id required"}, actions, [], None
            data = await self.woo.get_reviews(product_id)
            actions.append({"type": "show_reviews", "payload": {
                "product_id": product_id,
                "reviews": data.get("reviews", []),
                "average_rating": data.get("average_rating", 0),
                "count": data.get("count", 0),
            }})
            return data, actions, [], None

        if tool_name == "add_multiple_to_cart":
            items_to_add = tool_args.get("items") or []
            results = []
            for item in items_to_add[:5]:  # max 5 at once
                pid = self._safe_int(item.get("product_id"), 0)
                if not pid:
                    continue
                qty = max(1, self._safe_int(item.get("quantity"), 1))
                # Use client-side add_to_cart action (respects browser cookie session)
                actions.append({
                    "type": "add_to_cart",
                    "payload": {
                        "product_id": pid,
                        "variation_id": self._safe_int(item.get("variation_id"), 0),
                        "variation": item.get("attributes") or {},
                        "quantity": qty,
                    }
                })
                results.append({"product_id": pid, "queued": True})
            return {"results": results, "note": "client_side_action"}, actions, [], None

        return {"ignored_tool": tool_name}, actions, product_ids, customer_email

    async def _handle_product_discovery(self, message: str, lower: str, language: str) -> Dict[str, Any]:
        min_price, max_price = self._extract_budget(lower)
        query = self._normalize_discovery_query(message)
        wants_all = any(
            token in lower
            for token in [
                "all products",
                "all items",
                "entire catalog",
                "full catalog",
                "list all",
                "show all",
                "catalog",
            ]
        )
        limit = 24 if wants_all or not query else 8
        in_stock_only = False if wants_all or not query else ("out of stock" not in lower)
        products = await self.woo.search_products(
            query=query,
            min_price=min_price,
            max_price=max_price,
            in_stock_only=in_stock_only,
            limit=limit,
        )

        if not products:
            products = await self.woo.search_products(
                query="",
                min_price=min_price,
                max_price=max_price,
                in_stock_only=False,
                limit=24,
            )

        if not products:
            return {
                "response_text": self._say(language, "no_products"),
                "ui_actions": [],
                "suggested_replies": ["Show products", "Show my cart"],
            }

        # Pick best matching product and show it first, limit display to 6
        best = self._pick_best_product_match(lower, products)
        if best and best in products:
            products.remove(best)
            products.insert(0, best)
        products = products[:6]

        name = products[0].get("name", "")
        price = products[0].get("price", "")
        price_text = f", ₹{price}" if price else ""
        response = f"{name}{price_text} — want me to show the size and color options?"

        return {
            "response_text": response,
            "ui_actions": [{"type": "show_products", "payload": {"products": products}}],
            "suggested_replies": ["Show options", "Add to cart", "Show my cart"],
            "last_products": [p.get("id") for p in products if p.get("id")],
        }

    async def _handle_buy_intent(self, message: str, lower: str, session_id: str, language: str) -> Optional[Dict[str, Any]]:
        """Handle 'I want to buy X' — find the exact product and show variant picker."""
        # Strip buy-intent words to extract product name
        query = re.sub(
            r"\b(i want to|i'd like to|i would like to|want to|i want|i'll take|get me a?|buy me a?|buy|purchase|order)\b",
            "", message, flags=re.IGNORECASE
        ).strip()
        query = re.sub(r"\s+", " ", query).strip()

        if not query:
            return None

        products = await self.woo.search_products(query=query, in_stock_only=False, limit=4)
        if not products:
            return None

        product = self._pick_best_product_match(query, products) or products[0]
        product_id = product.get("id")
        name = product.get("name", "")
        price = product.get("price", "")
        price_text = f"₹{price}" if price else ""

        actions: List[Dict[str, Any]] = [{"type": "show_products", "payload": {"products": [product]}}]
        if product_id:
            actions.append({"type": "show_variant_picker", "payload": {"product_id": product_id}})

        response = f"{name}{', ' + price_text if price_text else ''}. Let me pull up the options for you."

        return {
            "response_text": response,
            "ui_actions": actions,
            "suggested_replies": ["Add to cart", "Show details", "Show my cart"],
            "last_products": [p.get("id") for p in products if p.get("id")],
        }

    async def _handle_availability(self, message: str, lower: str, last_products: List[Any], language: str) -> Optional[Dict[str, Any]]:
        size, color = self._extract_size_color(lower)
        query = self._normalize_availability_query(message)

        product: Optional[Dict[str, Any]] = None
        if query:
            rows = await self.woo.search_products(query=query, in_stock_only=False, limit=6)
            if rows:
                product = self._pick_best_product_match(query, rows)
        elif last_products:
            _lp0 = last_products[0]
            _lp_id = _lp0.get('id') if isinstance(_lp0, dict) else _lp0
            if _lp_id:
                detail = await self.woo.get_product_details(int(_lp_id))
                product = {
                    "id": detail.get("id"),
                    "name": detail.get("name"),
                    "price": detail.get("price"),
                    "stock_status": detail.get("stock_status"),
                }

        if not product or not product.get("id"):
            return {
                "response_text": self._say(language, "ask_product_for_stock"),
                "ui_actions": [],
                "suggested_replies": ["Show products"],
            }

        # Build attributes dict from extracted size/color
        attributes: Optional[Dict[str, str]] = None
        if size or color:
            attributes = {}
            if size:
                attributes["size"] = size
            if color:
                attributes["color"] = color

        inventory = await self.woo.check_inventory(
            product_id=int(product["id"]),
            attributes=attributes,
        )
        in_stock = bool(inventory.get("in_stock"))
        qty = inventory.get("stock_quantity")

        actions: List[Dict[str, Any]] = [
            {
                "type": "show_availability",
                "payload": {
                    "product": product,
                    "inventory": inventory,
                    "attributes": attributes or {},
                },
            }
        ]

        if not in_stock:
            similar = await self.woo.search_products(query=str(product.get("name") or ""), in_stock_only=True, limit=4)
            if similar:
                actions.append({"type": "show_products", "payload": {"products": similar}})

        return {
            "response_text": self._say(
                language,
                "availability",
                name=product.get("name", "Product"),
                size=size or "",
                qty=qty,
                in_stock=in_stock,
            ),
            "ui_actions": actions,
            "suggested_replies": ["Add to cart" if in_stock else "Show alternatives", "Show my cart"],
            "last_products": [product.get("id")],
        }

    async def _handle_compare(self, message: str, lower: str, last_products: List[Any], language: str) -> Optional[Dict[str, Any]]:
        terms = self._split_compare_terms(message)
        items: List[Dict[str, Any]] = []

        for term in terms:
            rows = await self.woo.search_products(query=term, in_stock_only=False, limit=1)
            if rows:
                row = rows[0]
                items.append(
                    {
                        "id": row.get("id"),
                        "name": row.get("name"),
                        "price": row.get("price"),
                        "sale_price": row.get("sale_price"),
                        "in_stock": self._in_stock(row),
                        "image_url": row.get("image_url") or (row.get("images", [{}])[0].get("src") if row.get("images") else ""),
                        "permalink": row.get("permalink", ""),
                    }
                )

        if len(items) < 2 and len(last_products) >= 2:
            for pid in last_products[:3]:
                detail = await self.woo.get_product_details(int(pid))
                items.append(
                    {
                        "id": detail.get("id"),
                        "name": detail.get("name"),
                        "price": detail.get("price"),
                        "sale_price": "",
                        "in_stock": self._in_stock(detail),
                        "image_url": detail.get("image_url") or (detail.get("images", [{}])[0].get("src") if detail.get("images") else ""),
                        "permalink": detail.get("permalink", ""),
                    }
                )

        deduped = []
        seen = set()
        for item in items:
            item_id = item.get("id")
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            deduped.append(item)

        if len(deduped) < 2:
            return {
                "response_text": self._say(language, "need_two_compare"),
                "ui_actions": [],
                "suggested_replies": ["Show products"],
            }

        return {
            "response_text": self._say(language, "comparison_ready"),
            "ui_actions": [{"type": "show_comparison", "payload": {"items": deduped[:3]}}],
            "suggested_replies": ["Add first one", "Check availability"],
            "last_products": [item.get("id") for item in deduped[:3]],
        }

    async def _handle_order_tracking(self, message: str, lower: str, state: Dict[str, Any], language: str) -> Optional[Dict[str, Any]]:
        email = self._extract_email(lower) or state.get("customer_email")
        if not email:
            return {
                "response_text": self._say(language, "ask_order_email"),
                "ui_actions": [],
                "suggested_replies": [],
            }

        orders = await self.woo.get_orders(customer_email=email, limit=5)
        if not orders:
            return {
                "response_text": self._say(language, "order_not_found"),
                "ui_actions": [],
                "suggested_replies": ["Show products"],
                "customer_email": email,
            }

        latest = orders[0]
        order_no = latest.get("order_number") or latest.get("order_id") or "-"
        status = latest.get("status", "processing")
        return {
            "response_text": self._say(language, "order_status", order_no=order_no, status=status),
            "ui_actions": [{"type": "show_orders", "payload": {"orders": orders}}],
            "suggested_replies": ["Show my cart", "Show products"],
            "customer_email": email,
        }

    async def _handle_add_to_cart(
        self,
        message: str,
        lower: str,
        session_id: str,
        last_products: List[Any],
        language: str,
    ) -> Optional[Dict[str, Any]]:
        qty = self._extract_quantity(lower)
        size, color = self._extract_size_color(lower)

        product = await self._resolve_product_for_add(message, lower, last_products)
        if not product or not product.get("id"):
            return {
                "response_text": self._say(language, "ask_add_which"),
                "ui_actions": [],
                "suggested_replies": ["Show products"],
            }

        variation_id = 0
        variation_data: Dict[str, Any] = {}

        # Build attributes dict from extracted size/color
        attributes: Optional[Dict[str, str]] = None
        if size or color:
            attributes = {}
            if size:
                attributes["size"] = size
            if color:
                attributes["color"] = color

        if attributes:
            inventory = await self.woo.check_inventory(product_id=int(product["id"]), attributes=attributes)
            variation_id = int(inventory.get("variation_id") or 0)
            variation_data = self.woo._attributes_to_variation_map(inventory.get("attributes", []))
            if not inventory.get("in_stock"):
                alternatives = await self.woo.search_products(query=str(product.get("name") or ""), in_stock_only=True, limit=4)
                actions = [
                    {
                        "type": "show_availability",
                        "payload": {
                            "product": product,
                            "inventory": inventory,
                            "attributes": attributes or {},
                        },
                    }
                ]
                if alternatives:
                    actions.append({"type": "show_products", "payload": {"products": alternatives}})
                return {
                    "response_text": self._say(language, "out_of_stock", name=product.get("name", "Product"), size=size or ""),
                    "ui_actions": actions,
                    "suggested_replies": ["Show alternatives"],
                    "last_products": [p.get("id") for p in alternatives if p.get("id")],
                }
            # We have a variation — if quantity not yet specified, ask how many
            if qty <= 0 or qty == 1:
                # Only ask quantity if it wasn't explicitly stated in the message
                if not re.search(r'\b(\d+)\s*(piece|pcs|qty|quantity|units?|nos?|number)?\b', lower):
                    product_name = product.get("name", "Product")
                    size_label = f" size {size}" if size else ""
                    color_label = f" {color}" if color else ""
                    return {
                        "response_text": f"Great choice!{color_label}{size_label} — How many {product_name} would you like to add?",
                        "ui_actions": [],
                        "suggested_replies": ["1", "2", "3"],
                        "last_products": [product.get("id")],
                        "_pending_add": {
                            "product_id": int(product["id"]),
                            "variation_id": variation_id,
                            "variation": variation_data,
                        },
                    }

        # If no explicit attributes were given, check if the product has variations
        # and ASK the user to choose instead of silently adding the wrong one
        if not attributes:
            detail = await self.woo.get_product_details(product_id=int(product["id"]))
            variations = await self.woo.find_variants(product_id=int(product["id"]))
            if variations and variations.get("variations"):
                return {
                    "response_text": f"Please select the specific options for {product.get('name', 'this product')} to add it to your cart.",
                    "ui_actions": [
                        {
                            "type": "show_variants",
                            "payload": {
                                "product": detail,
                                "variations": variations.get("variations", [])
                            }
                        },
                    ],
                    "suggested_replies": ["Show details", "Cancel"],
                    "last_products": [product.get("id")],
                }
            # Simple product (no variations) — if qty not given, ask
            if qty <= 0 or qty == 1:
                if not re.search(r'\b(\d+)\s*(piece|pcs|qty|quantity|units?|nos?|number)?\b', lower):
                    return {
                        "response_text": f"How many {product.get('name', 'items')} would you like to add to your cart?",
                        "ui_actions": [],
                        "suggested_replies": ["1", "2", "3"],
                        "last_products": [product.get("id")],
                    }

        # Final quantity — ensure at least 1
        final_qty = max(1, qty)
        return {
            "response_text": self._say(
                language,
                "added_to_cart",
                name=product.get("name", "Product"),
                qty=final_qty,
            ),
            "ui_actions": [
                {
                    "type": "add_to_cart",
                    "payload": {
                        "product_id": int(product["id"]),
                        "variation_id": variation_id,
                        "variation": variation_data,
                        "quantity": final_qty,
                    },
                },
            ],
            "suggested_replies": ["Add another item", "View cart", "Proceed to checkout"],
            "last_products": [product.get("id")],
        }

    async def _resolve_product_for_add(self, message: str, lower: str, last_products: List[Any]) -> Optional[Dict[str, Any]]:
        def _get_product_id(p: Any) -> Optional[int]:
            if isinstance(p, dict):
                pid = p.get('id')
            else:
                pid = p
            try:
                return int(pid) if pid else None
            except (TypeError, ValueError):
                return None

        if any(token in lower for token in ["add it", "add this", "add first", "yes add", "add one"]):
            if last_products:
                pid = _get_product_id(last_products[0])
                if pid:
                    detail = await self.woo.get_product_details(pid)
                    return {"id": detail.get("id"), "name": detail.get("name", "Product")}

        product_id_match = re.search(r"product\s*id\s*(\d+)", lower)
        if product_id_match:
            pid = int(product_id_match.group(1))
            detail = await self.woo.get_product_details(pid)
            if detail.get("id"):
                return {"id": detail.get("id"), "name": detail.get("name", "Product")}

        query = self._extract_add_query(message)
        if query:
            matches = await self.woo.search_products(query=query, in_stock_only=False, limit=6)
            if matches:
                return self._pick_best_product_match(query, matches)

        if last_products:
            pid = _get_product_id(last_products[0])
            if pid:
                detail = await self.woo.get_product_details(pid)
                return {"id": detail.get("id"), "name": detail.get("name", "Product")}

        return None

    async def _safe_get_cart(self, session_id: str) -> Dict[str, Any]:
        try:
            cart = await self.woo.get_live_cart(session_id=session_id)
            await self.session.save_cart(session_id, cart)
            return cart
        except Exception as e:
            logger.warning(f"Live cart fetch failed, using cache: {e}")
            cart = await self.session.get_cart(session_id)
            if cart and not cart.get("is_empty", True):
                return cart
            return {"is_empty": True, "items": [], "total": "₹0", "item_count": 0}

    @staticmethod
    def _normalize_cart_payload(cart: Dict[str, Any]) -> Dict[str, Any]:
        item_count = int(cart.get("item_count") or cart.get("count") or 0)
        return {
            "is_empty": item_count == 0,
            "item_count": item_count,
            "total": str(cart.get("total") or "₹0"),
            "items": cart.get("items") or [],
        }

    @staticmethod
    def _tool_schema() -> List[Dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "search_products",
                    "description": "Search for products by name, brand, category, price, and attributes. Use the 'brand' parameter when customer asks for a specific brand (e.g. Nike, Adidas). If brand returns no results, re-search without brand to find similar alternatives.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Product name or keyword to search for"},
                            "brand": {"type": "string", "description": "Brand name filter (e.g. 'Nike', 'Adidas'). Include brand name in query as well for best results."},
                            "category": {"type": "string"},
                            "min_price": {"type": "number"},
                            "max_price": {"type": "number"},
                            "in_stock_only": {"type": "boolean"},
                            "limit": {"type": "integer"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_product_details",
                    "description": (
                        "Get full details for a product including all variants, sizes, colors, and images. "
                        "IMPORTANT: You MUST call search_products first to get the numeric product_id. "
                        "Pass product_id as an INTEGER number (e.g. 123), NOT a product name string."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "product_id": {
                                "type": "integer",
                                "description": "The numeric product ID (integer) obtained from search_products result. NOT a product name."
                            },
                        },
                        "required": ["product_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "check_inventory",
                    "description": "Check if a specific product variant (color + size combination) is in stock. Pass attributes as a key-value dict, e.g. {\"color\": \"red\", \"size\": \"M\"}. Returns in_stock, stock_quantity, and the matched variation_id. If variant_not_found is true in the response, that exact combo does not exist — call find_variants to see what IS available.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "product_id": {"type": "integer"},
                            "variation_id": {"type": "integer", "description": "Optional: specific variation ID if already known"},
                            "attributes": {"type": "object", "description": "Key-value pair of variation attributes, e.g. {\"color\": \"red\", \"size\": \"M\"}"},
                        },
                        "required": ["product_id"],
                    },
                },
            },
            {"type": "function", "function": {"name": "get_cart", "description": "Get customer cart", "parameters": {"type": "object", "properties": {}}}},
            {
                "type": "function",
                "function": {
                    "name": "add_to_cart",
                    "description": "Add a product to cart with optional variant and quantity.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "product_id": {"type": "integer"},
                            "variation_id": {"type": "integer"},
                            "quantity": {"type": "integer"},
                            "attributes": {"type": "object", "description": "Key-value pair of variation attributes selected by customer"},
                        },
                        "required": ["product_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "remove_from_cart",
                    "description": "Remove cart item by cart_item_key.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "cart_item_key": {"type": "string"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_orders",
                    "description": "Get recent orders by customer email.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "customer_email": {"type": "string"},
                        },
                        "required": ["customer_email"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "apply_coupon",
                    "description": "Apply discount coupon to cart.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "coupon_code": {"type": "string"},
                        },
                        "required": ["coupon_code"],
                    },
                },
            },
            {"type": "function", "function": {"name": "get_categories", "description": "Get product categories.", "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "get_store_info", "description": "Get store policies and capabilities.", "parameters": {"type": "object", "properties": {}}}},
            {
                "type": "function",
                "function": {
                    "name": "compare_products",
                    "description": (
                        "Compare 2-3 products side by side. PREFERRED: pass product_ids as a list of integers "
                        "from prior search results. Fallback: pass product_a and product_b as search strings."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "product_ids": {
                                "type": "array",
                                "items": {"type": "integer"},
                                "description": "List of 2-3 numeric product IDs to compare (preferred over product_a/product_b)",
                            },
                            "product_a": {"type": "string", "description": "Product name to search (fallback)"},
                            "product_b": {"type": "string", "description": "Product name to search (fallback)"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_reviews",
                    "description": "Get customer reviews and ratings for a product. Use when customer asks about reviews, ratings, feedback, or wants to know if a product is good. After fetching, summarise naturally: mention the average rating, what customers consistently praise, and any common complaints — like a friend summarising word-of-mouth. Never read out individual reviews verbatim.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "product_id": {"type": "integer", "description": "Numeric product ID"},
                        },
                        "required": ["product_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "add_multiple_to_cart",
                    "description": "Add multiple products to cart in one go. Use when customer wants to buy several items at once.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "items": {
                                "type": "array",
                                "description": "List of products to add",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "product_id": {"type": "integer"},
                                        "quantity": {"type": "integer"},
                                        "attributes": {"type": "object"},
                                    },
                                    "required": ["product_id"],
                                },
                            },
                        },
                        "required": ["items"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "update_cart_quantity",
                    "description": "Update the quantity of an item in the cart.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "product_id": {"type": "integer"},
                            "quantity": {"type": "integer"},
                        },
                        "required": ["product_id", "quantity"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "find_variants",
                    "description": "Get all variations for a variable product with stock per variant. Critical for size/color selection.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "product_id": {"type": "integer"},
                        },
                        "required": ["product_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_best_coupon",
                    "description": "Find the best available coupon for the customer (discount amount/description). no arguments needed.",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "submit_review",
                    "description": "Submit a product review.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "product_id": {"type": "integer"},
                            "rating": {"type": "integer", "description": "Rating out of 5"},
                            "review": {"type": "string"},
                            "name": {"type": "string"},
                        },
                        "required": ["product_id", "rating", "review", "name"],
                    },
                },
            },
        ]

    @staticmethod
    def _normalize_discovery_query(message: str) -> str:
        cleaned = re.sub(
            r"\b(show|find|search|products?|items?|available|availability|compare|cart|checkout|please|i need|i want|looking for|list|what are|that|this|those|these|the|a|an|give me|get me|want|is|are|do|does|can|have|has|there|any|which|tell|me|about|you|in|stock|check|do you|looking|for|what|which|see|any|some)\b",
            " ",
            message.lower(),
        )
        cleaned = re.sub(r"\b(under|below|less than|above|over|more than)\s*\d+(?:\.\d+)?\b", " ", cleaned)
        return re.sub(r"\s+", " ", cleaned).strip()

    @staticmethod
    def _normalize_availability_query(message: str) -> str:
        cleaned = re.sub(r"\b(do you have|is|available|availability|in stock|stock|size\s*[a-z0-9.-]+|check)\b", " ", message.lower())
        return re.sub(r"\s+", " ", cleaned).strip()

    @staticmethod
    def _extract_add_query(message: str) -> str:
        cleaned = re.sub(r"\b(add|to|cart|please|qty|quantity|size\s*[a-z0-9.-]+|color\s*[a-z-]+|my|the|in|into)\b", " ", message.lower())
        cleaned = re.sub(r"[\"']", " ", cleaned)
        cleaned = re.sub(r"\b\d+\b", " ", cleaned)
        return re.sub(r"\s+", " ", cleaned).strip()

    @staticmethod
    def _split_compare_terms(message: str) -> List[str]:
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

    @staticmethod
    def _extract_budget(lower: str) -> Tuple[Optional[float], Optional[float]]:
        max_match = re.search(r"(?:under|below|less than|upto|up to)\s*(\d+(?:\.\d+)?)", lower)
        min_match = re.search(r"(?:above|over|more than)\s*(\d+(?:\.\d+)?)", lower)
        return (float(min_match.group(1)) if min_match else None, float(max_match.group(1)) if max_match else None)

    @staticmethod
    def _extract_quantity(lower: str) -> int:
        # Match quantity-specific patterns first (avoid matching product IDs, prices, sizes)
        qty_pattern = re.search(
            r'\b(\d{1,2})\s*(?:piece|pcs|qty|quantity|units?|nos?|number|pairs?|sets?)\b'
            r'|(?:buy|add|get|want|need|take|order)\s+(\d{1,2})\b',
            lower
        )
        if qty_pattern:
            val = int(qty_pattern.group(1) or qty_pattern.group(2))
            if 1 <= val <= 20:
                return val
        return 1

    @staticmethod
    def _extract_size_color(lower: str) -> Tuple[Optional[str], Optional[str]]:
        # Named patterns: "size XL", "color red", "in red", "the blue one"
        size_match = re.search(r"\b(?:size|sized?)\s*([a-z0-9.\-]+)", lower)
        color_match = re.search(r"\b(?:color|colour|in)\s+([a-z]+)\b", lower)

        # Standalone size keywords (XS/S/M/L/XL/XXL or numeric like 10, 42)
        _SIZES = {"xs", "s", "m", "l", "xl", "xxl", "xxxl", "2xl", "3xl",
                  "small", "medium", "large", "xsmall", "xsm", "xlarge"}
        # Standalone color keywords
        _COLORS = {"red", "blue", "green", "black", "white", "yellow", "pink",
                   "orange", "purple", "grey", "gray", "gold", "silver", "brown",
                   "navy", "maroon", "violet", "cyan", "beige", "cream", "khaki"}

        size = size_match.group(1).strip() if size_match else None
        color = color_match.group(1).strip() if color_match else None

        if not size:
            words = lower.split()
            for w in words:
                if w in _SIZES:
                    size = w
                    break

        if not color:
            words = lower.split()
            for w in words:
                if w in _COLORS:
                    color = w
                    break

        return size, color

    @staticmethod
    def _extract_email(lower: str) -> Optional[str]:
        match = re.search(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", lower)
        return match.group(0) if match else None

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except Exception:
            return default

    @staticmethod
    def _safe_optional_int(value: Any) -> Optional[int]:
        try:
            if value in (None, "", 0, "0"):
                return None
            return int(value)
        except Exception:
            return None

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            if value in (None, ""):
                return None
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _extract_inline_function_calls(content: str) -> Tuple[List[Tuple[str, Dict[str, Any]]], str]:
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

        # Pattern 2: function_name({"key": "value"}) — Python-call style
        py_pattern = re.compile(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(\s*(\{[^}]{0,400}\})\s*\)')
        _KNOWN_TOOLS = {
            "search_products", "get_product_details", "check_inventory",
            "add_to_cart", "add_multiple_to_cart", "remove_from_cart", "get_cart",
            "get_orders", "apply_coupon", "get_categories", "get_store_info",
            "compare_products", "get_reviews", "find_variants", "get_best_coupon",
            "update_cart_quantity", "submit_review",
        }
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
            cleaned
        )

        # Pattern 3: {"type": "function", "name": "tool_name", "parameters"|"arguments": {...}}
        # Some models (Cerebras, llama) output tool calls as JSON objects in text content
        # Handles both "parameters" and "arguments" key variants, with {} for no-arg tools
        type_fn_pattern = re.compile(
            r'\{\s*"type"\s*:\s*"function"\s*,\s*"name"\s*:\s*"([a-zA-Z0-9_]+)"\s*(?:,\s*"(?:parameters|arguments)"\s*:\s*(\{[^{}]*\}))?\s*\}',
            re.DOTALL
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
            cleaned
        )

        # Pattern 4: {"name": "tool_name", "arguments": {...}} — OpenAI-style text leak
        name_args_pattern = re.compile(
            r'\{\s*"name"\s*:\s*"([a-zA-Z0-9_]+)"\s*,\s*"arguments"\s*:\s*(\{[^{}]*\})\s*\}',
            re.DOTALL
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
            cleaned
        )

        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return calls, cleaned

    @staticmethod
    def _extract_next_suggestions(text: str) -> tuple[list[str], str]:
        """Extract NEXT: suggestion line from LLM response.
        Returns (suggestions_list, cleaned_text_without_next_line).
        """
        if not text:
            return [], text
        # Match "NEXT: option1 | option2" at end of text (case-insensitive)
        pattern = re.compile(r'\n?NEXT\s*:\s*(.+)$', re.IGNORECASE | re.MULTILINE)
        match = pattern.search(text)
        suggestions: list[str] = []
        if match:
            raw = match.group(1)
            suggestions = [s.strip() for s in raw.split("|") if s.strip()][:3]
            text = pattern.sub("", text).strip()
        return suggestions, text

    @staticmethod
    def _cap_to_sentences(text: str, max_sentences: int = 4) -> str:
        """
        Hard-cap the response to max_sentences for voice call appropriateness.
        Splits on sentence-ending punctuation, keeps the first N, re-joins cleanly.
        """
        if not text:
            return text
        # Split on sentence boundaries
        parts = re.split(r'(?<=[.!?])\s+', text.strip())
        parts = [p.strip() for p in parts if p.strip()]
        if len(parts) <= max_sentences:
            return text
        truncated = " ".join(parts[:max_sentences])
        # Ensure it ends with punctuation
        if truncated and truncated[-1] not in ".!?":
            truncated += "."
        return truncated

    @staticmethod
    def _strip_function_markup(text: str) -> str:
        cleaned = str(text or "")
        # Strip Qwen-3 / other model thinking/reasoning blocks
        cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL)
        cleaned = re.sub(r"<reasoning>.*?</reasoning>", "", cleaned, flags=re.DOTALL)
        # Remove <function=name {...}></function> XML-style tags
        cleaned = re.sub(r"<function\s*=\s*([a-zA-Z0-9_]+)\s*({.*?})\s*</function>", "", cleaned, flags=re.DOTALL)
        cleaned = re.sub(r"<function[^>]*>", "", cleaned, flags=re.DOTALL)
        cleaned = cleaned.replace("</function>", "")
        # Remove function_name({...}) Python-call style leaks
        cleaned = re.sub(r'\b[a-zA-Z_][a-zA-Z0-9_]*\s*\(\s*\{[^}]{0,300}\}\s*\)', "", cleaned)
        # Remove standalone JSON objects that contain known tool argument keys
        _TOOL_KEYS = r'(?:query|product_id|product_ids|category|limit|min_price|max_price|in_stock_only|cart_item_key|attributes|quantity|coupon_code|email|compare_ids|order_id)'
        cleaned = re.sub(r'\{[^{}]{0,400}' + _TOOL_KEYS + r'[^{}]{0,400}\}', "", cleaned, flags=re.DOTALL)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    @staticmethod
    def _summarize_actions_for_voice(actions: List[Dict[str, Any]]) -> str:
        if not actions:
            return "I can help with products, availability, cart, and checkout."

        # Priority pass: availability, variants, cart, orders, add_to_cart
        # These are more specific than show_products and should be voiced first.
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

        # Secondary pass: product display (less specific)
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

    @staticmethod
    def _in_stock(row: Optional[Dict[str, Any]]) -> bool:
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

    @staticmethod
    def _pick_best_product_match(query: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
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
            score = 0
            for term in terms:
                if term in name:
                    score += 2
            if needle and name.startswith(needle):
                score += 3
            if score > best_score:
                best_score = score
                best = row

        return best

    @staticmethod
    def _has_buy_intent(lower: str) -> bool:
        """Catches 'I want to buy X' — distinct from add-to-cart (which needs a product already shown)."""
        return any(token in lower for token in [
            "i want to buy", "i'd like to buy", "want to buy",
            "i want to purchase", "i'd like to purchase", "want to purchase",
            "i want to order", "i'll take", "get me a", "buy me a",
        ])

    @staticmethod
    def _has_add_intent(lower: str) -> bool:
        return any(token in lower for token in [
            "add to cart", "add this to cart", "add it to cart",
            "buy this", "yes add", "put in cart", "add one",
            "add it", "add this", "yes, add",
        ])

    @staticmethod
    def _has_remove_intent(lower: str) -> bool:
        return any(token in lower for token in [
            "remove", "delete from cart", "delete item", "delete product", "delete this",
        ])

    @staticmethod
    def _has_cart_view_intent(lower: str) -> bool:
        return any(token in lower for token in [
            "show cart", "my cart", "view cart", "cart total", "open cart",
        ]) or lower.strip() == "cart"

    @staticmethod
    def _has_checkout_intent(lower: str) -> bool:
        return any(token in lower for token in ["checkout", "proceed to checkout", "buy now", "place order", "order now"])

    @staticmethod
    def _has_compare_intent(lower: str) -> bool:
        return "compare" in lower or " vs " in lower or " versus " in lower

    @staticmethod
    def _has_inventory_intent(lower: str) -> bool:
        catalog_query = bool(
            re.search(r"(show|list|what|which).*(available).*(product|products)", lower)
            or re.search(r"available\s+products?", lower)
        )
        if catalog_query:
            return False
        return any(token in lower for token in ["availability", "in stock", "stock", "size ", "do you have"])

    @staticmethod
    def _has_order_intent(lower: str) -> bool:
        # Only match order TRACKING intent, not purchase intent
        tracking_tokens = ["track my order", "where is my order", "order status", "my order",
                           "track order", "order tracking", "order delivered", "order shipped"]
        if any(token in lower for token in tracking_tokens):
            return True
        # "order" alone only if NOT combined with purchase verbs
        purchase_words = ["want to order", "want to buy", "i'll order", "i will order",
                          "place order", "order now", "order a ", "order the "]
        if any(w in lower for w in purchase_words):
            return False
        return False  # Default: don't capture generic "order" — LLM handles it better

    @staticmethod
    def _has_store_info_intent(lower: str) -> bool:
        tokens = [
            "store info",
            "store name",
            "shop name",
            "what is this store",
            "what's this store",
            "what are store name",
            "what is store name",
            "name of store",
            "who are you",
            "about this store",
            "store details",
            "about store",
            "store information",
            "tell me about",
            "about your shop",
            "about the shop",
            "about the store",
            "shop info",
            "shop details",
        ]
        if any(token in lower for token in tokens):
            return True
        return bool(re.search(r"\b(store|shop)\b.*\bname\b", lower))

    @staticmethod
    def _has_shipping_intent(lower: str) -> bool:
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

    @staticmethod
    def _has_returns_intent(lower: str) -> bool:
        tokens = [
            "return policy", "returns policy", "refund policy",
            "can i return", "can i exchange", "how to return", "how to refund",
            "return product", "exchange product",
            "return period", "return window", "money back",
            "what is your return", "what is the return",
        ]
        return any(t in lower for t in tokens)

    @staticmethod
    def _has_payment_intent(lower: str) -> bool:
        tokens = [
            "payment method", "payment option", "how to pay", "how can i pay",
            "accepted payment", "pay online", "pay by card", "pay by upi",
            "do you accept", "cash on delivery", "cod", "credit card", "debit card",
        ]
        return any(t in lower for t in tokens)

    @staticmethod
    def _normalize_language(language: Optional[str]) -> str:
        raw = str(language or "en").strip().lower()
        if raw in _SUPPORTED_LANGS:
            return raw
        for lang in _SUPPORTED_LANGS:
            if raw.startswith(lang):
                return lang
        return "en"

    @staticmethod
    def _with_actions_alias(payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return payload
        ui = payload.get("ui_actions")
        if "actions" not in payload and isinstance(ui, list):
            payload["actions"] = ui
        return payload

    @staticmethod
    def _say(language: str, key: str, **kwargs: Any) -> str:
        templates = {
            "en": {
                "checkout_triggered": "Sure. Let me collect your delivery details first.",
                "cart_opened": "Here is your current cart.",
                "cart_empty": "Your cart is empty right now.",
                "removed_from_cart": "Removed {name} from your cart.",
                "no_products": "I couldn't find a close match. Try product name, brand, size, or budget.",
                "products_found": "{name} looks like your best bet. Want me to show the size and color options?",
                "ask_product_for_stock": "Tell me the product name and size, and I'll check live stock.",
                "availability": "{name} {size_text}is {stock_text}{qty_text}.",
                "need_two_compare": "Please name at least two products to compare.",
                "comparison_ready": "I compared the options for you. Do you want me to add one to cart?",
                "store_info": "This store is {store_name}. I can help you find products, compare options, and checkout faster.",
                "ask_order_email": "Please share your order email and I'll fetch your latest status.",
                "order_not_found": "I couldn't find recent orders for that email.",
                "order_status": "Your latest order #{order_no} is {status}.",
                "ask_add_which": "Tell me which product to add, or say add the first one.",
                "out_of_stock": "{name} {size_text}is out of stock. I can show similar options.",
                "added_to_cart": "Done! {name} (qty: {qty}) is in your cart. Would you like to add anything else or proceed to checkout?",
                "ask_variation": "{name} comes in these options: {options}. Which one would you like, and how many?",
            },
            "hi": {
                "checkout_triggered": "Bilkul. Chaliye delivery details lete hain.",
                "cart_opened": "Yeh aapka current cart hai.",
                "cart_empty": "Aapka cart abhi khaali hai.",
                "removed_from_cart": "Maine {name} cart se hata diya.",
                "no_products": "Exact match nahi mila. Product name, brand ya budget boliye.",
                "products_found": "{name} best option lagta hai. Size aur color options dikhaaun?",
                "ask_product_for_stock": "Product ka naam aur size batayein, main live stock check karta hoon.",
                "availability": "{name} {size_text}{stock_text}{qty_text}.",
                "need_two_compare": "Compare ke liye kam se kam do products boliye.",
                "comparison_ready": "Maine options compare kar diye. Kya ek cart mein add kar doon?",
                "store_info": "Is store ka naam {store_name} hai. Main products dhoondne, compare karne, aur checkout mein help kar sakta hoon.",
                "ask_order_email": "Order status ke liye apna email batayein.",
                "order_not_found": "Is email ke liye recent order nahi mila.",
                "order_status": "Aapka latest order #{order_no} abhi {status} hai.",
                "ask_add_which": "Kaunsa product add karna hai? Ya bolo first wala add karo.",
                "out_of_stock": "{name} {size_text}stock mein nahi hai. Similar options dikhaun?",
                "added_to_cart": "Done! {name} ({qty} nos.) cart mein add ho gaya. Kuch aur add karein ya checkout karein?",
                "ask_variation": "{name} yeh options mein available hai: {options}. Kaunsa chahiye aur kitne?",
            },
            "ml": {
                "checkout_triggered": "ശരി. Delivery details edukkam.",
                "cart_opened": "Ithaa ningalude cart.",
                "cart_empty": "Cart ippol khaaliyannu.",
                "removed_from_cart": "{name} cart-il ninnum neekkiyirikkunnu.",
                "no_products": "Onnum kittiyilla. Product peru, brand, athava budget parayoo.",
                "products_found": "{name} best option aanu. Size, color options kaanano?",
                "ask_product_for_stock": "Product peru, size parayoo — stock check cheyyam.",
                "availability": "{name} {size_text}{stock_text}{qty_text}.",
                "need_two_compare": "Compare cheyyaan rantu products parayoo.",
                "comparison_ready": "Njaan options compare cheythu. Onnu cart-il ittekkaano?",
                "store_info": "{store_name} aanu ee store. Products kandittu, compare cheythu, checkout cheyyaan help cheyyam.",
                "ask_order_email": "Order status ariyaan email parayoo.",
                "order_not_found": "Aa email-il order kittiyilla.",
                "order_status": "Ningalude latest order #{order_no} ippol {status} aanu.",
                "ask_add_which": "Ethu product add cheyyano? First onnu parayoo.",
                "out_of_stock": "{name} {size_text}stock illaa. Similar options kaanano?",
                "added_to_cart": "Done! {name} ({qty} nos.) cart-il undi. Ingane mattonninum veno, checkout cheyyano?",
                "ask_variation": "{name} ithaa options: {options}. Ethu veno, etthu veno?",
            },
            "ta": {
                "checkout_triggered": "Sari. Delivery details vaangurom.",
                "cart_opened": "Ingae ungal cart irukku.",
                "cart_empty": "Cart ippo kaaliyannu.",
                "removed_from_cart": "{name} cart-la irundhu eduthutten.",
                "no_products": "Onnum kanavillai. Product peyar, brand, athava budget sollunga.",
                "products_found": "{name} best choice. Size, color options paakalamaa?",
                "ask_product_for_stock": "Product peyar, size sollunga — stock check pannuven.",
                "availability": "{name} {size_text}{stock_text}{qty_text}.",
                "need_two_compare": "Compare panna rendu products sollunga.",
                "comparison_ready": "Nangu compare panniten. Onnu cart-la podalaamaa?",
                "store_info": "Ithu {store_name}. Products thedu, compare pannu, checkout aaga help pannuven.",
                "ask_order_email": "Order status therinja email sollunga.",
                "order_not_found": "Aa email-la order kanavillai.",
                "order_status": "Ungal recent order #{order_no} ippo {status} la irukku.",
                "ask_add_which": "Etha product add pannanum? First onnu sollunga.",
                "out_of_stock": "{name} {size_text}stock-la illai. Similar options paakalamaa?",
                "added_to_cart": "Aayittu! {name} ({qty} nos.) cart-la irukku. Vera enna venom, checkout panalaamaa?",
                "ask_variation": "{name}-ku ithu options: {options}. Etha venom, evvalavu venom?",
            },
            "te": {
                "checkout_triggered": "Sare. Delivery details teesukuntam.",
                "cart_opened": "Meeru cart ivigo.",
                "cart_empty": "Cart ipudu khaaliganundi.",
                "removed_from_cart": "{name} cart nundi teesaanu.",
                "no_products": "Emi dorkaledu. Product peru, brand, budget cheppandi.",
                "products_found": "{name} best option. Size, color options chupimma?",
                "ask_product_for_stock": "Product peru, size cheppandi — stock check chestanu.",
                "availability": "{name} {size_text}{stock_text}{qty_text}.",
                "need_two_compare": "Compare cheyyataniki rendu products cheppandi.",
                "comparison_ready": "Nannu options compare chesanu. Okati cart lo veyyanaa?",
                "store_info": "Idi {store_name}. Products vethakataniki, compare cheyyataniki, checkout ki help chestanu.",
                "ask_order_email": "Order status kosam email cheppandi.",
                "order_not_found": "Aa email ki order dorkaledu.",
                "order_status": "Meeru recent order #{order_no} ipudu {status} lo undi.",
                "ask_add_which": "Etha product add cheyyali? Modati okati cheppandi.",
                "out_of_stock": "{name} {size_text}stock lo ledu. Similar options chupimma?",
                "added_to_cart": "Aipoyindi! {name} ({qty} nos.) cart lo undi. Inkemi veladam, checkout chesdam?",
                "ask_variation": "{name} ki ee options unnai: {options}. Edu kavali, enni kavali?",
            },
            "bn": {
                "checkout_triggered": "Thik ache. Delivery details neoa jak.",
                "cart_opened": "Ei je aapnar cart.",
                "cart_empty": "Aapnar cart ekhon khali.",
                "removed_from_cart": "{name} cart theke sore diyechi.",
                "no_products": "Kono mael paoaa jaini. Product naam, brand ba budget bolun.",
                "products_found": "{name} best option. Size, color options dekhabo?",
                "ask_product_for_stock": "Product naam, size bolun — stock check korbo.",
                "availability": "{name} {size_text}{stock_text}{qty_text}.",
                "need_two_compare": "Compare korar jonno duto product bolun.",
                "comparison_ready": "Ami options compare korechi. Ekta cart e debo?",
                "store_info": "Ei store er naam {store_name}. Products khuje, compare kore, checkout e help korbo.",
                "ask_order_email": "Order status er jonno email bolun.",
                "order_not_found": "Oi email e order paoaa jaini.",
                "order_status": "Aapnar recent order #{order_no} ekhon {status} e ache.",
                "ask_add_which": "Kon product add korbo? Prothomta bolun.",
                "out_of_stock": "{name} {size_text}stock e nei. Similar option dekhabo?",
                "added_to_cart": "Hoyeche! {name} ({qty} nos.) cart e ache. Ar kichu lagbe, checkout korben?",
                "ask_variation": "{name} r ei options ache: {options}. Konta lagbe, koto lagbe?",
            },
            "kn": {
                "checkout_triggered": "Sari. Delivery details tegedukoLona.",
                "cart_opened": "Nimage cart illi ide.",
                "cart_empty": "Cart ippudu khaali agi ide.",
                "removed_from_cart": "{name} cart ninda bitti.",
                "no_products": "Yenu sikkililla. Product hesaru, brand, athava budget heli.",
                "products_found": "{name} best choice. Size, color options nodona?",
                "ask_product_for_stock": "Product hesaru, size heli — stock check madutta.",
                "availability": "{name} {size_text}{stock_text}{qty_text}.",
                "need_two_compare": "Compare maadalu eradu products heli.",
                "comparison_ready": "Naanu options compare madide. Ondu cart ge hako?",
                "store_info": "Ee store hesaru {store_name}. Products houdi, compare maadi, checkout ge help madutta.",
                "ask_order_email": "Order status ge email heli.",
                "order_not_found": "Aa email ge order sikkililla.",
                "order_status": "Nimage recent order #{order_no} ippudu {status} alli ide.",
                "ask_add_which": "Yaavudu product add maadali? Modalu ondu heli.",
                "out_of_stock": "{name} {size_text}stock alli illa. Similar options nodona?",
                "added_to_cart": "Aayitu! {name} ({qty} nos.) cart alli ide. Innu bere beku, checkout maadona?",
                "ask_variation": "{name} ge ee options idhe: {options}. Yaavudu beku, eshtu beku?",
            },
        }
        table = templates.get(language, templates["en"])
        tpl = table.get(key, templates["en"].get(key, ""))

        size = str(kwargs.get("size") or "").strip()
        qty = kwargs.get("qty")
        in_stock = kwargs.get("in_stock")
        kwargs["size_text"] = (f"size {size} " if size else "")
        if in_stock is None:
            kwargs["stock_text"] = ""
            kwargs["qty_text"] = ""
        else:
            kwargs["stock_text"] = "is available" if in_stock else "is currently unavailable"
            kwargs["qty_text"] = f" with only {qty} left" if isinstance(qty, int) else ""

        try:
            return tpl.format(**kwargs)
        except KeyError:
            return tpl
