"""Address collection state machine."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from ...core.security import sanitize_text
from .text_utils import speech_digits_to_ascii, normalize_india_state, extract_email


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
        return all([self.first_name, self.last_name, self.address_line1, self.city, self.postcode, self.phone])

    def to_woocommerce_format(self) -> Dict[str, str]:
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


async def handle_address_collection(
    session_id: str,
    user_message: str,
    current_state: str,
    address_data: dict,
    language: str,
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
        addr.state = normalize_india_state(cleaned)
        next_state = AddressCollectionState.COLLECTING_PINCODE
        response = lang_prompts["pincode"]

    elif current_state == AddressCollectionState.COLLECTING_PINCODE:
        numbers = re.findall(r"\d+", speech_digits_to_ascii(cleaned).replace(" ", ""))
        pincode = "".join(numbers)[:6]
        if len(pincode) == 6:
            addr.postcode = pincode
            next_state = AddressCollectionState.COLLECTING_PHONE
            response = lang_prompts["phone"]
        else:
            response = "I need a 6-digit PIN code. Could you repeat it?"

    elif current_state == AddressCollectionState.COLLECTING_PHONE:
        numbers = re.findall(r"\d+", speech_digits_to_ascii(cleaned).replace(" ", ""))
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
        if any(token in lowered for token in affirmative):
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
        else:
            next_state = AddressCollectionState.COLLECTING_NAME
            response = "No problem, let's start over. " + lang_prompts["name"]

    return response, next_state, addr.__dict__, ui_actions
