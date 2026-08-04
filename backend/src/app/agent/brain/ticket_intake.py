"""Deterministic ticket-intake FSM + escalation trigger (no LLM).

Drives support escalation end-to-end:
  1. `detect_escalation()` — keyword match on complaint / talk-to-human tokens,
     run BEFORE the LLM so a mis-routed (e.g. Malayalam) damaged-order complaint
     escalates deterministically instead of getting a product-search answer.
  2. If the customer's contact info is already known (session meta), a ticket is
     created immediately (de-duplicated per session within 60 minutes).
  3. Otherwise the turn is OWNED by the intake FSM: Aria asks for an email or
     phone number and creates the ticket on the follow-up message.

State is persisted in session meta under `ticket_intake_state` /
`ticket_intake_pending` (same pattern as the address FSM in address.py).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from .text_utils import extract_email, normalize_phone_digits
from ..guardrails import extract_contact_info, is_pii_placeholder
from ...services.ticketing import (
    _HIGH_PRIORITY_TOKENS,
    _LOW_PRIORITY_TOKENS,
    create_support_ticket,
    ticket_created_message,
)

logger = logging.getLogger(__name__)


class TicketIntakeState:
    IDLE = "idle"
    AWAITING_CONTACT = "awaiting_contact"


# ── Escalation trigger ────────────────────────────────────────────────────────

# Pure informational / policy Q&A must NOT escalate — the normal store-info / LLM
# flow answers those ("what is your return policy?", "how long is a refund?").
_ESCALATION_EXCLUDE_RE = re.compile(
    r"\b(policy|what if|what happens if|how long|"
    r"do you (accept|have|offer|allow|do|give)|"
    r"can i return|can i get a refund|can you tell me|can you (do|help|tell)|"
    r"how (do|can|to) (i|you|we) (return|refund|exchange|get|track)|"
    r"what is your|what's your|what are your|do you have a|is there a|"
    r"does your store|your return|your refund)\b",
    re.IGNORECASE,
)

# "damage-resistant", "damage proof", "damage guard/cover/shield" are marketing
# terms, not complaints — keep those on the search path.
_DAMAGE_MARKETING_RE = re.compile(
    r"damage[-\s]?(?:resistant|proof|protection|guard|shield|cover)", re.IGNORECASE
)

_ESCALATION_TOKENS = tuple(set(_HIGH_PRIORITY_TOKENS) | set(_LOW_PRIORITY_TOKENS))


def detect_escalation(text: str) -> bool:
    """True when `text` reads like a support escalation, not a policy question."""
    low = " ".join(str(text or "").lower().split())
    if not low:
        return False
    if _ESCALATION_EXCLUDE_RE.search(low):
        return False
    if _DAMAGE_MARKETING_RE.search(low):
        return False
    return any(tok in low for tok in _ESCALATION_TOKENS)


def session_contact(session_meta: Any, state: Any = None) -> Tuple[str, str]:
    """Return (email, phone) known from session meta / state, placeholders cleared.

    Redaction placeholders (`[email]` / `[phone]`) are treated as unknown — the
    server-side real values live under `customer_email` / `customer_phone`.
    """
    meta = session_meta if isinstance(session_meta, dict) else {}
    state = state if isinstance(state, dict) else {}
    email = str(meta.get("customer_email") or state.get("customer_email") or "").strip().lower()
    phone = str(meta.get("customer_phone") or "").strip()
    addr = meta.get("address_data")
    if isinstance(addr, dict):
        phone = phone or str(addr.get("phone") or "").strip()
        email = email or str(addr.get("email") or "").strip().lower()
    if is_pii_placeholder(email):
        email = ""
    if is_pii_placeholder(phone):
        phone = ""
    return email, phone


# ── Contact prompts (language-aware) ──────────────────────────────────────────

_ASK_CONTACT_TEXTS = {
    "en": "I can raise a support ticket for that right away. So our team can reach you, could you share your email address or phone number?",
    "hi": "Main turant aapke liye support ticket bana sakta hoon. Hamari team aapse baat karne ke liye, kya aap apna email address ya phone number bata sakte hain?",
    "ml": "Ningalude appeal-inaayi njaan ippol thanne oru support ticket undaakkaam. Njangalude team-innu ninne bandhappedaan, ningalude email allengil phone number parayaamo?",
    "ta": "Ungal kavalai-kkaga naan udane support ticket edukka mudiyum. Engal team ungalai thodarpadukka, ungal email allathu phone number sollunga?",
    "te": "Mee vinathi kosam nenu ippude support ticket tayaru cheyagalanu. Maa team mee kontact-kosam, mee email lekundaa phone number cheppagalara?",
    "bn": "Ami ekhanei apanr jonno support ticket tairi korte pari. Amaader team apnar sathe jogajog korar jonno, apnar email ba phone number bollun?",
    "kn": "Nimma vinathi-kkagi naanu ippale support ticket madabahudu. Namma team nim'manu samparkisalu, nimma email athava phone number heLiri?",
}

_REASK_CONTACT_TEXTS = {
    "en": "I still need a way for our team to contact you. Could you share your email address or phone number?",
    "hi": "Hamari team ko aapse baat karne ke liye abhi bhi ek raasta chahiye. Kya aap apna email address ya phone number de sakte hain?",
    "ml": "Njangalude team-ine ninne bandhappedaan iniyum oru margham venam. Ningalude email allengil phone number parayaamo?",
    "ta": "Engal team ungalai thodarpadukka innum oru vaazhi venum. Ungal email allathu phone number sollunga?",
    "te": "Maa team mee kontact-kosam inka oka maargam kavali. Mee email lekundaa phone number cheppagalara?",
    "bn": "Amaader team apnar sathe jogajog korar jonno ekta upay dorkar. Apnar email ba phone number bollun?",
    "kn": "Namma team nim'manu samparkisalu innu ondu maargavella beku. Nimma email athava phone number heLiri?",
}

_CANCEL_TEXTS = {
    "en": "No problem — I'm here if you need anything else.",
    "hi": "Koi baat nahi — kuch aur chahiye ho toh main yahin hoon.",
    "ml": "Vendaa — vere enthengilum venamenkil njaan ive unde.",
    "ta": "Seri — vera edhavadhu venum-na naan inga irukken.",
    "te": "Sare — inkemaina kavali ante nenu ikkade unnanu.",
    "bn": "Kono somossa nei — ar kichu lagle ami achi.",
    "kn": "Parvagilla — bere enadru beku andre naanu ilddane iddene.",
}

# Cancelling an in-progress intake ("never mind", "stop", "nothing"…).
_CANCEL_RE = re.compile(
    r"\b(never\s*?mind|cancel|stop|skip|forget it|leave it|quit|nothing|"
    r"no need|its ok|it's ok|no thanks|koi baat nahi|nahi chahiye|venda|nillu|sare)\b",
    re.IGNORECASE,
)


def _localized(mapping: Dict[str, str], language: str) -> str:
    return mapping.get(language, mapping["en"])


def ask_contact_prompt(language: str) -> str:
    return _localized(_ASK_CONTACT_TEXTS, language)


def reask_contact_prompt(language: str) -> str:
    return _localized(_REASK_CONTACT_TEXTS, language)


# ── Intake turn handler ───────────────────────────────────────────────────────

async def handle_ticket_intake_turn(
    *,
    cleaned_message: str,
    session_meta: Any,
    tenant_id: str,
    session_id: str,
    conversation_history: Optional[List[Dict[str, Any]]],
    store_client: Any,
    session_service: Any,
    language: str,
) -> Tuple[str, str, Dict[str, Any], List[Dict[str, Any]]]:
    """Process one intake reply → (text, next_state, pending, ui_actions).

    While `awaiting_contact`, extracts an email or phone from the reply; on
    success it creates the ticket (de-duplicated) and returns the confirmation +
    a `show_ticket` ui_action. On a cancellation it resets the FSM to idle.
    """
    pending = (session_meta or {}).get("ticket_intake_pending") or {}
    if not isinstance(pending, dict) or not pending.get("trigger_message"):
        # Stale/unknown state — reset to idle rather than asking forever.
        return _localized(
            {"en": "How can I help you?", "hi": "Main aapki kya madad kar sakta hoon?",
             "ml": "Njan ningalkku engane sahaayikkan?", "ta": "Naalu ungalukku enna help pannalum?",
             "te": "Nenu meeku e vidanga sahayam cheyalenu?", "bn": "Ami apnake ki bhabe shahajjo korte pari?",
             "kn": "Naanu nimagenu sahayavagabahudu?"}, language), TicketIntakeState.IDLE, {}, []

    low = " ".join(str(cleaned_message or "").lower().split())
    if _CANCEL_RE.search(low):
        logger.info("Ticket intake cancelled session=%s", session_id)
        return _localized(_CANCEL_TEXTS, language), TicketIntakeState.IDLE, {}, []

    email, phone = extract_contact_info(cleaned_message or "")
    if not email:
        email = str(extract_email(low) or "").strip().lower()
    if not phone:
        digits = normalize_phone_digits(cleaned_message or "")
        if len(digits) >= 10:
            phone = digits[-10:]

    # `cleaned_message` has already been PII-redacted by check_input (email →
    # [email]), so the real values only live in session meta — captured from the
    # RAW message in brain/core.py Step 1. Fall back to those or we'd re-ask
    # forever and never create the ticket.
    if not email or not phone:
        known_email, known_phone = session_contact(session_meta)
        if not email:
            email = known_email
        if not phone:
            phone = known_phone

    if not email and not phone:
        return reask_contact_prompt(language), TicketIntakeState.AWAITING_CONTACT, pending, []

    logger.info(
        "Ticket intake complete session=%s email=%s phone=%s (creating ticket)",
        session_id, email or "-", phone or "-",
    )
    ticket_result = await create_support_ticket(
        tenant_id=tenant_id,
        session_id=session_id,
        conversation_history=conversation_history or [],
        store_client=store_client,
        session_service=session_service,
        customer_email=email,
        customer_phone=phone,
        trigger_message=str(pending.get("trigger_message") or ""),
        product_id=pending.get("product_id"),
        source="deterministic",
    )

    actions: List[Dict[str, Any]] = []
    if ticket_result.get("status") == "success":
        ticket_number = str(ticket_result.get("ticket_number") or "")
        actions.append({
            "type": "show_ticket",
            "payload": {
                "ticket_id": ticket_result["ticket_id"],
                "ticket_number": ticket_number,
                "priority": ticket_result.get("priority", "medium"),
                "heat": ticket_result.get("heat"),
                "message": ticket_created_message(language, ticket_number),
            },
        })
    text = str(ticket_result.get("message") or "") or ticket_created_message(language)
    return text, TicketIntakeState.IDLE, {}, actions
