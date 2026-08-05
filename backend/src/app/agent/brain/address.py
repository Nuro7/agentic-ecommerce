"""Address collection state machine."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ...core.security import sanitize_text
from .text_utils import (
    speech_digits_to_ascii,
    normalize_phone_digits,
    normalize_province_code,
    normalize_country_code,
    extract_email,
)


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
    COLLECTING_UPDATE_FIELD = "collecting_update_field"
    COLLECTING_UPDATE_VALUE = "collecting_update_value"


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
    # Non-persisted-to-checkout working flags for the returning-customer flow.
    _using_saved: str = ""
    _pending_field: str = ""

    def is_complete(self) -> bool:
        # Phone is optional in the US flow — a customer may skip it.
        return all([self.first_name, self.last_name, self.address_line1, self.city, self.postcode])

    @property
    def _store_country(self) -> str:
        return os.getenv("STORE_COUNTRY", "US")

    def to_woocommerce_format(self) -> Dict[str, str]:
        country = self._store_country
        return {
            "first_name": self.first_name,
            "last_name": self.last_name,
            "address_1": self.address_line1,
            "city": self.city,
            "state": self.state,
            "postcode": self.postcode,
            "country": country,
            "phone": self.phone,
            "email": self.email,
            "state_code": normalize_province_code(self.state, country),
            "country_code": normalize_country_code(country, default="US"),
        }


# ── Returning-customer partial-update helpers ────────────────────────────────

# Conversational filler a voice/user utterance may wrap a name in. Stripped
# BEFORE splitting into first/last so "my name is John Doe" yields
# first_name="John", last_name="Doe" (not first="My", last="name is John Doe").
_NAME_FILLER_RE = re.compile(
    r"^\s*(?:my\s+)?(?:full\s+)?name\s+is\s+|i\s+am\s+|i'm\s+|this\s+is\s+"
    r"|its\s+|it's\s+|the\s+name\s+is\s+|im\s+|i\s*am\s*",
    re.IGNORECASE,
)


def _clean_full_name(text: str) -> str:
    """Strip leading conversational filler and collapse inner whitespace."""
    cleaned = _NAME_FILLER_RE.sub("", str(text or ""))
    return re.sub(r"\s{2,}", " ", cleaned).strip()


# Map a user-spoken detail onto an AddressData field + the prompt key that asks
# for its value. `tokens` is matched as a substring of the lowercased utterance.
_FIELD_SPECS = [
    ("last_name", "last name, surname, family name, sure name", "last_name"),
    ("first_name", "first name, name, first, naam, peru", "name"),
    ("address_line1", "address, home, house, street, line 1, address line", "address"),
    ("city", "city, town, district, nagar", "city"),
    ("state", "state, province, region", "state"),
    ("postcode", "pincode, pin, zip, postal, postcode", "pincode"),
    ("phone", "phone, mobile, number, contact", "phone"),
    ("email", "email, mail", "email"),
]


def _detect_address_field(lower: str) -> str:
    """Return the canonical AddressData field the customer wants to update, or ''."""
    for field, tokens, _prompt_key in _FIELD_SPECS:
        if any(token in lower for token in tokens.split(", ")):
            return field
    return ""


def _update_value_prompt(lang_prompts: dict, field: str) -> str:
    """Prompt to collect the new value for `field`, reusing existing per-feature text."""
    key = {
        "first_name": "name", "last_name": "last_name", "address_line1": "address",
        "city": "city", "state": "state", "postcode": "pincode",
        "phone": "phone", "email": "email",
    }.get(field, "address")
    return "OK. " + lang_prompts.get(key, "Please tell me the new value.")


def _apply_update_value(addr: AddressData, field: str, cleaned: str) -> Tuple[bool, str]:
    """Set only the requested field. Returns (ok, response_or_error_msg)."""
    low = cleaned.lower()
    if field == "first_name":
        if cleaned.strip():
            addr.first_name = cleaned.strip().split()[0]
            return True, "Got it."
        return False, "Please tell me the first name."
    if field == "last_name":
        parts = cleaned.strip().split()
        if len(parts) >= 1:
            addr.last_name = " ".join(parts[:2])
            return True, "Got it."
        return False, "Please tell me the last name."
    if field == "address_line1":
        if cleaned.strip():
            addr.address_line1 = cleaned.strip()
            return True, "Got it."
        return False, "Please tell me the address."
    if field == "city":
        if cleaned.strip():
            addr.city = cleaned.strip()
            return True, "Got it."
        return False, "Please tell me the city."
    if field == "state":
        st = normalize_province_code(cleaned, country=os.getenv("STORE_COUNTRY", "US"))
        if st:
            addr.state = st
            return True, "Got it."
        return False, "I couldn't read that state. Please tell me again."
    if field == "postcode":
        digits = normalize_phone_digits(cleaned)
        # US ZIP codes are 5 digits; other countries 6. Accept either.
        if len(digits) >= 5:
            addr.postcode = digits[:5] if os.getenv("STORE_COUNTRY", "US") == "US" else digits[:6]
            return True, "Got it."
        return False, "I need a valid US ZIP code (5 digits). Please repeat it."
    if field == "phone":
        lowered = cleaned.lower()
        if any(t in lowered for t in ["skip", "no phone", "no number", "i dont", "don't have one", "none", "no thanks"]):
            addr.phone = ""
            return True, "Got it."
        digits = normalize_phone_digits(cleaned)
        if len(digits) >= 10:
            addr.phone = digits[-10:]
            return True, "Got it."
        return False, "I need a 10-digit phone number, or say skip."
    if field == "email":
        if "skip" in low or "no email" in low:
            addr.email = ""
            return True, "Got it."
        email = extract_email(low)
        if email:
            addr.email = email
            return True, "Got it."
        return False, "Please tell a valid email address, or say skip."
    return False, "Please tell me the value."


_PROMPTS: Dict[str, Dict[str, str]] = {
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
        "update_field": "Which detail would you like to change - name, address, city, state, pincode, phone, or email?",
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
        "update_field": "Kaun si detail badalni hai - naam, address, city, state, pincode, phone ya email?",
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
        "update_field": "Etu detail maata vaanao - name, address, city, state, zip, phone, email?",
    },
}


async def handle_address_collection(
    session_id: str,
    user_message: str,
    current_state: str,
    address_data: dict,
    language: str,
    page_context: Optional[Dict[str, Any]] = None,
    tenant_id: Optional[str] = None,
) -> Tuple[str, str, dict, List[Dict[str, Any]]]:
    lang_prompts = _PROMPTS.get(language, _PROMPTS["en"])
    addr = AddressData()
    if isinstance(address_data, dict):
        for key, value in address_data.items():
            if hasattr(addr, key):
                setattr(addr, key, str(value or "").strip())

    next_state = current_state
    response = ""
    ui_actions: List[Dict[str, Any]] = []
    cleaned = sanitize_text(user_message or "", max_len=250)

    # Checkout phone-first mode: when the customer is on a checkout page, or has
    # just asked to buy now / go to checkout / place an order, and the state is
    # IDLE, start collecting the shipping details (phone first). Detect checkout
    # from the explicit page_type OR the URL (/checkout) because the widget's
    # Shopify analytics pageType is unreliable on the checkout route; buy-now is
    # recognised by the same checkout-intent phrases the brain uses so a "buy it
    # now" from a product page begins the guided address flow instead of jumping.
    _CL = str(user_message or "").strip().lower()
    _pg = page_context or {}
    is_checkout = (
        _pg.get("page_type") == "checkout"
        or "/checkout" in str(_pg.get("url") or "").lower()
    )
    _had_checkout_intent = bool(re.search(
        r"\b(buy it now|buy-now|buy now|bought|checkout|check\s*out|place\s+(?:the\s+)?order|"
        r"order\s+now|proceed\s+to\s+checkout|go\s+to\s+checkout|complete\s+(?:my\s+)?(?:order|purchase)|"
        r"pay(?:ment)?\s+now|finish\s+(?:my\s+)?(?:order|purchase))\b",
        _CL,
    ))
    if (is_checkout or _had_checkout_intent) and current_state == AddressCollectionState.IDLE:
        next_state = AddressCollectionState.COLLECTING_PHONE
        response = "What's your phone number for shipping updates?"
        return response, next_state, addr.__dict__, ui_actions

    # COMPLETE re-entry: the flow already collected a full address in a previous
    # turn but the redirect to checkout hasn't landed yet (e.g. the customer is
    # back on the cart / product page and says "proceed to checkout" again). Re-issue
    # the Storefront-checkout redirect with the stored address instead of returning
    # an EMPTY response — an empty result made the voice model hallucinate the
    # navigation ("taking you to checkout") while nothing actually happened.
    if current_state == AddressCollectionState.COMPLETE and (
        addr.address_line1 and addr.city and addr.postcode
    ):
        response = lang_prompts["done"]
        ui_actions.append({
            "type": "redirect_checkout_with_address",
            "payload": {
                "url": "/checkout",
                "billing": addr.to_woocommerce_format(),
                "shipping": addr.to_woocommerce_format(),
            },
        })
        return response, next_state, addr.__dict__, ui_actions

    if current_state == AddressCollectionState.COLLECTING_NAME:
        parts = _clean_full_name(cleaned).split(maxsplit=1)
        addr.first_name = parts[0] if parts else ""
        if len(parts) > 1:
            addr.last_name = parts[1]
            next_state = AddressCollectionState.COLLECTING_ADDRESS_LINE1
            response = lang_prompts["address"]
        else:
            next_state = AddressCollectionState.COLLECTING_LAST_NAME
            response = lang_prompts["last_name"]

    elif current_state == AddressCollectionState.COLLECTING_LAST_NAME:
        last_name = _clean_full_name(cleaned)
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
        addr.state = normalize_province_code(cleaned, country=os.getenv("STORE_COUNTRY", "US"))
        next_state = AddressCollectionState.COLLECTING_PINCODE
        response = lang_prompts["pincode"]

    elif current_state == AddressCollectionState.COLLECTING_PINCODE:
        numbers = normalize_phone_digits(cleaned)
        # US ZIP codes are 5 digits (occasionally 5+4); other countries 6.
        # Accept 5 or 6 digits and keep the leading 5 for the US.
        pincode = numbers[:6]
        if len(pincode) in (5, 6):
            addr.postcode = pincode[:5] if os.getenv("STORE_COUNTRY", "US") == "US" else pincode[:6]
            # Phone-first flow: phone already collected (or skipped), so proceed
            # to collect the email next.
            next_state = AddressCollectionState.COLLECTING_EMAIL
            response = lang_prompts["email"]
        else:
            response = "I need a valid US ZIP code (5 digits). Could you repeat it?"

    elif current_state == AddressCollectionState.COLLECTING_PHONE:
        lowered = cleaned.lower()
        # Phone is optional in the US flow — customer may skip it.
        if any(t in lowered for t in ["skip", "no phone", "no number", "i dont", "don't have one", "none", "no thanks"]):
            addr.phone = ""
            next_state = AddressCollectionState.COLLECTING_NAME
            response = lang_prompts["name"]
        else:
            # Normalize FIRST: strip every non-digit, convert spoken digit words
            # ("nine eight seven...") and formatted numbers ("+91 987-654-3210")
            # into a pure digit string BEFORE any 10-digit constraint is evaluated.
            phone = normalize_phone_digits(cleaned)
            if len(phone) >= 10:
                addr.phone = phone[-10:]

                # Look up the saved address by phone whenever the checkout flow is
                # running (buy-now intent OR checkout page). If a DB match exists we
                # BYPASS the rest of the address collection: confirm the saved
                # address and immediately issue the Storefront checkout redirect.
                if tenant_id:
                    try:
                        from ...modules.users.address_service import get_address_by_phone
                        saved = await get_address_by_phone(addr.phone, tenant_id)
                    except Exception:
                        saved = None
                    if saved:
                        addr.first_name = saved.get("first_name") or addr.first_name
                        addr.last_name = saved.get("last_name") or addr.last_name
                        addr.address_line1 = saved.get("address_1") or addr.address_line1
                        addr.city = saved.get("city") or addr.city
                        addr.state = saved.get("state") or addr.state
                        addr.postcode = saved.get("postcode") or addr.postcode
                        addr.email = saved.get("email") or addr.email
                        addr._using_saved = "1"
                        addr._pending_field = ""

                        # Saved address is complete enough to ship → show it for
                        # CONFIRMATION (never jump straight to checkout). The
                        # customer verifies the details, then the CONFIRMING branch
                        # issues the Storefront checkout redirect on "yes".
                        if addr.address_line1 and addr.city and addr.postcode:
                            addr._using_saved = "1"
                            next_state = AddressCollectionState.CONFIRMING
                            response = lang_prompts["confirm"].format(
                                name=f"{addr.first_name} {addr.last_name}".strip(),
                                address=addr.address_line1,
                                city=addr.city,
                                pincode=addr.postcode,
                                phone=addr.phone,
                                email=addr.email or "not provided",
                            )
                            ui_actions.append({
                                "type": "prefill_address",
                                "payload": addr.to_woocommerce_format(),
                            })
                            return response, next_state, addr.__dict__, ui_actions

                        # Partial saved record (no address/city) → fall through and
                        # collect the missing full address for the first time.
                        addr._using_saved = ""

                # Normal flow (phone-first): collect the rest of the delivery details —
                # name → address → city → state → pincode → email → confirm. We never
                # skip straight to email on the buy-now path because the collected
                # address is what gets bound to the Storefront cart / saved to DB.
                next_state = AddressCollectionState.COLLECTING_NAME
                response = lang_prompts["name"]
            else:
                # A short/non-phone answer (not a skip phrase) — phone is optional,
                # so proceed without it rather than block the checkout flow.
                next_state = AddressCollectionState.COLLECTING_NAME
                response = lang_prompts["name"]

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
            ui_actions.append({"type": "prefill_address", "payload": addr.to_woocommerce_format()})
        else:
            email = extract_email(lowered)
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
                ui_actions.append({"type": "prefill_address", "payload": addr.to_woocommerce_format()})
            else:
                response = "Please tell a valid email address, or say skip."

    elif current_state == AddressCollectionState.CONFIRMING:
        affirmative = {
            "yes", "yeah", "yep", "yup", "ok", "okay", "sure", "correct",
            "right", "of course", "certainly", "absolutely", "definitely",
            "go ahead", "go", "proceed", "confirm", "confirmed", "done",
            "perfect", "alright", "fine", "great", "sounds good", "do it",
            "let's go", "lets go", "place order", "pay now",
            "haan", "ha", "acha", "theek", "bilkul", "zaroor", "karo",
            "seri", "aayi", "sheriyanu", "sheriya", "ittekkaamo",
            "sari", "aamam", "seyyungal",
            "avunu", "sare", "cheyyi",
        }
        lowered = cleaned.lower()
        if any(re.search(rf"\b{re.escape(t)}\b", lowered) for t in affirmative):
            next_state = AddressCollectionState.COMPLETE
            response = lang_prompts["done"]
            ui_actions.append({
                "type": "redirect_checkout_with_address",
                "payload": {
                    "url": "/checkout",
                    "billing": addr.to_woocommerce_format(),
                    "shipping": addr.to_woocommerce_format(),
                },
            })
            # Save address for future lookups
            if tenant_id:
                try:
                    from ...modules.users.address_service import save_address
                    await save_address(
                        session_id=session_id,
                        tenant_id=tenant_id,
                        phone=addr.phone,
                        address_data=addr.to_woocommerce_format(),
                    )
                except Exception:
                    pass
        else:
            # Returning customer who already prefilled from a saved address →
            # offer a targeted update that KEEPS the rest of the saved details.
            if getattr(addr, "_using_saved", "") == "1":
                addr._pending_field = ""
                next_state = AddressCollectionState.COLLECTING_UPDATE_FIELD
                response = lang_prompts["update_field"]
            else:
                next_state = AddressCollectionState.COLLECTING_NAME
                response = "No problem, let's start over. " + lang_prompts["name"]

    elif current_state == AddressCollectionState.COLLECTING_UPDATE_FIELD:
        lowered = cleaned.lower()
        field = _detect_address_field(lowered)
        if field:
            addr._pending_field = field
            next_state = AddressCollectionState.COLLECTING_UPDATE_VALUE
            response = _update_value_prompt(lang_prompts, field)
        elif any(
            re.search(rf"\b{re.escape(t)}\b", lowered)
            for t in [
                "yes", "yeah", "yep", "correct", "right", "ok", "okay", "sure",
                "proceed", "go ahead", "place order", "pay now", "confirmed", "done",
                "use it", "use this", "same", "haan", "ha", "seri", "sari", "aamam",
                "ente pora", "porady",
            ]
        ) or "no " in lowered and any(
            t in lowered for t in ["update", "change", "nothing", "no update"]
        ):
            # "Yes / correct / proceed" with a saved address (or "no updates
            # needed") → finish checkout. If the customer said "no" to the saved
            # address but wants updates, the field-detection above handled it.
            next_state = AddressCollectionState.COMPLETE
            response = lang_prompts["done"]
            ui_actions.append({
                "type": "redirect_checkout_with_address",
                "payload": {
                    "url": "/checkout",
                    "billing": addr.to_woocommerce_format(),
                    "shipping": addr.to_woocommerce_format(),
                },
            })
            if tenant_id:
                try:
                    from ...modules.users.address_service import save_address
                    await save_address(
                        session_id=session_id,
                        tenant_id=tenant_id,
                        phone=addr.phone,
                        address_data=addr.to_woocommerce_format(),
                    )
                except Exception:
                    pass
        else:
            response = lang_prompts["update_field"]

    elif current_state == AddressCollectionState.COLLECTING_UPDATE_VALUE:
        field = getattr(addr, "_pending_field", "") or ""
        if not field:
            addr._pending_field = ""
            next_state = AddressCollectionState.COLLECTING_UPDATE_FIELD
            response = lang_prompts["update_field"]
        else:
            ok, msg = _apply_update_value(addr, field, cleaned)
            if not ok:
                response = msg
            else:
                # Keep other saved fields untouched; just re-confirm & prefilled.
                addr._pending_field = ""
                next_state = AddressCollectionState.CONFIRMING
                response = lang_prompts["confirm"].format(
                    name=f"{addr.first_name} {addr.last_name}".strip(),
                    address=addr.address_line1,
                    city=addr.city,
                    pincode=addr.postcode,
                    phone=addr.phone,
                    email=addr.email or "not provided",
                )
                ui_actions.append({
                    "type": "prefill_address",
                    "payload": addr.to_woocommerce_format(),
                })

    return response, next_state, addr.__dict__, ui_actions
