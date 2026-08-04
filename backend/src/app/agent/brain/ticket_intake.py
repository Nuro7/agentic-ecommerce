"""Deterministic ticket-intake FSM + escalation trigger (no LLM).

Drives support escalation end-to-end:
  1. `detect_escalation()` — keyword match on complaint / talk-to-human tokens,
     run BEFORE the LLM so a mis-routed (e.g. Malayalam) damaged-order complaint
     escalates deterministically instead of getting a product-search answer.
  2. Contact collection is gated by a VERIFICATION step: a phone number is
     normalized to digits, validated to >= 10 digits, then read back and must be
     confirmed ("yes"/"correct") before the ticket is persisted — so corrupt /
     mis-transcribed numbers never reach `voice_tickets`.
  3. While the FSM owns the turn, product/catalog search is locked out (the brain
     returns the FSM response and never falls through to search / LLM).

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
    _PII_PLACEHOLDERS,
    _SPECIFIC_ISSUE_WORDS,
    build_final_issue_summary,
    create_support_ticket,
    ticket_created_message,
)

logger = logging.getLogger(__name__)


class TicketIntakeState:
    IDLE = "idle"
    AWAITING_CONTACT = "awaiting_contact"
    VERIFYING_PHONE = "verifying_phone"
    AWAITING_NAME = "awaiting_name"
    AWAITING_ISSUE = "awaiting_issue"


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

# Read-back confirmation: "Got it. That's 8-9-4-3-7-3-7-2-2-7, correct?"
_VERIFY_PHONE_TEXTS = {
    "en": "Got it. So that's {digits}, correct?",
    "hi": "Samajh gaya. To yah hai {digits}, kya yeh sahi hai?",
    "ml": "Manassilaayi. Ennaal athu {digits}, sheriyaano?",
    "ta": "Puriyuthu. Adhu {digits}, sariya?",
    "te": "Arthamaindi. Adi {digits}, correct ah?",
    "bn": "Bujhechi. Tai ei {digits}, ki thik?",
    "kn": "Arthavayitu. Adu {digits}, sariya?",
}

# The number they gave was too short / unparseable.
_INVALID_PHONE_TEXTS = {
    "en": "I didn't catch that properly. Please share a valid 10-digit mobile number, or your email address instead.",
    "hi": "Mujhe woh number thik se samajh nahi aaya. Kripya ek valid 10-digit mobile number, ya apna email address dijiye.",
    "ml": "Aa number enikku kurachu thettiyayi. Dayavayi onnu sheriyaya 10-digit mobile number, allengil email address parayamo?",
    "ta": "Antha number enakku sariyaga puriyala. Oru valid 10-digit mobile number, allathu ungal email address sollunga.",
    "te": "Aa number naaku sariyaga ardham kaaledu. Dayachesi oka valid 10-digit mobile number, lekundaa mee email address cheppandi.",
    "bn": "Number-ta ami thik bhabe bujhini. Onugolpo ekta valid 10-digit mobile number, ba apnar email address din.",
    "kn": "A number nanage sariyagi artha aagalilla. Dayavittu ondu valid 10-digit mobile number, allavaada nimma email address hELi.",
}

# Ask what the problem actually is — the customer's own words become the
# persisted `issue_summary` and drive priority/heat (no chat-history guessing).
_ASK_ISSUE_TEXTS = {
    "en": "Great! Briefly, what issue would you like our team to help you with?",
    "hi": "Badhiya! Sankhipt mein bataiye, team ko kis samasya mein aapki madad karni hai?",
    "ml": "Kollam! Njan chirathil parayaam, njangalude team ninkku entha pradeepnathathil sahaayikkanam?",
    "ta": "Nandri! Chiriththakama sollunga, engal team ungalukku enna issue-la help pannanum?",
    "te": "Baagundi! Konchamga cheppandi, maa team mee problem lo e vidanga help cheyali?",
    "bn": "Bhalo! Sanchhipte bollun, apnar kon samasya-te amaader team sahayjo korbe?",
    "kn": "Chennagide! Sankshipthavagi heLiri, namma team nimage ena samasye-alli sahayavagabeku?",
}

# Re-ask the issue when the reply came back empty / unparseable.
_REASK_ISSUE_TEXTS = {
    "en": "Could you briefly tell me what issue you're facing, so I can log it for our team?",
    "hi": "Kya aap sankhipt mein bata sakte hain ki kis samasya ka saamna kar rahe hain, taaki main ise team ke liye log kar sakun?",
    "ml": "Ningalude pradeepnathamaaya prashnam enthaanennu chirathil parayaamo, njaan athu team-inaayi log cheyyaan?",
    "ta": "Oru chiriththaka sollunga enna issue face panringa, adh-ai naan engal team-ukku log pannura madhiri?",
    "te": "Konthamga cheppara mee problem ento, naa team kosam log cheyataniki?",
    "bn": "Apnar ki somosya ta ar sammukhin hocchen, amader team er jonno log korar jonyo ekta kotha bollen?",
    "kn": "Nimage ena samasya iddhe antha sankshipthavagi heLiri, naa team-gagi log madalu?",
}

# STEP 3 — ask for the customer's name (one field per prompt, human-like flow).
_ASK_NAME_TEXTS = {
    "en": "Great! Who do I have the pleasure of speaking with?",
    "hi": "Badhiya! Main kis se baat kar raha hoon?",
    "ml": "Kollam! Njan aarodu saambhashikkukayaanu?",
    "ta": "Nandri! Naan yarodu pesi kondu irukken?",
    "te": "Baagundi! Nenu evaritho matladutunnanu?",
    "bn": "Bhalo! Ami kar sathe kotha boli?",
    "kn": "Chennagide! Naanu yarondige matanadutiddene?",
}

# Re-ask name when the reply was empty / looked like DOM noise, not a name.
_REASK_NAME_TEXTS = {
    "en": "I didn't quite catch that. Could you tell me your name?",
    "hi": "Mujhe thik se samajh nahi aaya. Kya aap apna naam bata sakte hain?",
    "ml": "Njan sheriykk manassilaayilla. Ningalude peru parayaamo?",
    "ta": "Naan sariyaga puriyala. Ungal peru sollunga?",
    "te": "Nenu sariyaga artham cheyyalekapoyanu. Mee peru cheppara?",
    "bn": "Ami thik kore bujhini. Apnar naam bollun?",
    "kn": "Nanu sariyagi artha madikollalilla. Nimma hesaru heLiri?",
}

# DOM layout noise that must NEVER be stored as a customer name — the widget
# occasionally leaks structural labels (Footer/Header/Navigation/Main) into the
# captured text. Strictly blacklisted + filtered before persisting.
_DOM_NAME_NOISE = {
    "footer", "header", "navigation", "main", "sidebar", "banner", "menu",
    "cart", "checkout", "search", "login", "signup", "register", "home",
    "product", "products", "shop", "store", "page", "content", "wrapper",
    "container", "hero", "featured", "section", "div", "span", "button",
    "link", "modal", "popup", "topnav", "navmenu", "hero", "carousel",
    "grid", "card", "footer-nav", "main-nav", "skip", "skip-link",
}

# A valid personal name: letters, spaces, apostrophes, hyphens, dots. Nothing
# else (no digits, no tags, no punctuation soup).
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z' .-]{1,39}$")

# Sentence-y fragments that are never a personal name. `_NAME_RE` is purely
# alphabetic so it happily matches "problem with my last order" — a customer who
# restates their issue instead of answering the name prompt must never have that
# sentence persisted as their name.
_SENTENCE_NAME_TOKENS = frozenset({
    "i", "my", "our", "your", "the", "a", "an", "and", "with", "to", "of",
    "it", "this", "that", "for", "on", "in", "is", "are", "was", "were",
    "have", "has", "had", "do", "did", "not", "no", "want", "need", "please",
    "problem", "issue", "order", "orders", "damaged", "damage", "broken",
    "defective", "refund", "received", "arrived", "delivery", "delivered",
    "item", "items", "last", "connect", "customer", "care", "talk", "human",
    "help", "support", "agent", "ticket", "raise", "can", "you", "me",
})

# Name-intro patterns (multi-language) → capture group is the candidate name.
_NAME_CAPTURE_RE = re.compile(
    r"\b(i'?m|my name is|call me|it'?s|this is|"
    r"(?:mera|apna|amar|nanna|naa)\s+naam|"
    r"(?:en|ente|naa|nanna)\s+peru|"
    r"naam\s+hai|peru\s+enthaan|peru|"
    r"(?:njaan|enthe)\s+peru|naanu|nanu|nene|naano)\s+"
    r"([A-Za-z][A-Za-z' .-]{1,39})",
    re.IGNORECASE,
)

# When the user politely skips giving a name ("not needed", "skip", "guest").
_SKIP_NAME_RE = re.compile(
    r"\b(no name|skip|guest|not needed|no thanks|don'?t ask|naam nahi|"
    r"venda|nillu|sare|nahi chahiye)\b",
    re.IGNORECASE,
)

# Ticket confirmed WITH the customer's own issue summary echoed back and the
# callback channel/contact they gave — matches the "TK-1001 / 'summary' / call
# you at <phone>" confirmation the user expects to see.
_TICKET_CREATED_WITH_ISSUE_TEXTS = {
    "en": "Thank you! I have created ticket #{number} with your issue summary: '{summary}'. Our support team will {callback} shortly.",
    "hi": "Dhanyavaad! Maine #{number} ticket bana diya hai aapke issue summary ke saath: '{summary}'. Hamari team jald hi {callback} karegi.",
    "ml": "Nanni! Njaan #{number} ticket undaakki, ningalude issue summary: '{summary}'. Njangalude team thazhottu {callback} cheyyum.",
    "ta": "Nandri! Ungal issue summary-odu #{number} ticket uraakkiyirukkirrom: '{summary}'. Engal team seegram {callback} pannum.",
    "te": "Dhanyavadalu! Mee issue summary-tho #{number} ticket tayaru chesanu: '{summary}'. Maa team tvaralo {callback} chestundi.",
    "bn": "Dhonnobad! Apanr issue summary shoho #{number} ticket tairi korechi: '{summary}'. Amaader team shigghiri {callback} korbe.",
    "kn": "Dhanyavaada! Nimma issue summary-jothe #{number} ticket madidde: '{summary}'. Namma team bega {callback} maadutte.",
}

# Contact callback phrasing per channel ("call you at 8943737227" / "email you
# at a@b.co") — keeps the confirmation human and echoes the verified contact.
_CALLBACK_PHRASES = {
    "call": {
        "en": "call you at {contact}",
        "hi": "aapko {contact} par call karegi",
        "ml": "ningale {contact} il vidikkum",
        "ta": "ungalai {contact} la call pannum",
        "te": "mee {contact} ki call chestundi",
        "bn": "apnake {contact} e call korbe",
        "kn": "nimmannu {contact} ge call maadutte",
    },
    "email": {
        "en": "email you at {contact}",
        "hi": "aapko {contact} par email karegi",
        "ml": "ningalude {contact} il email ayakkum",
        "ta": "ungalukku {contact} ku email pannum",
        "te": "mee {contact} ki email chestundi",
        "bn": "apnake {contact} e email korbe",
        "kn": "nimmannu {contact} ge email maadutte",
    },
}

# Positive confirmation the read-back number is correct.
_CONFIRM_RE = re.compile(
    r"\b(yes|yeah|yep|correct|right|that's? right|that is right|"
    r"sahi|haan|theek hai|sheri|sari|avun|correct ah|thik hai|theek)\b",
    re.IGNORECASE,
)

# User says the number was wrong / wants to give a new one.
_DENY_RE = re.compile(
    r"\b(no|nope|nah|wrong|not correct|not right|alla|venda|nahi|"
    r"edit|change|let me (say|give|type)|try again)\b",
    re.IGNORECASE,
)

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


def verify_phone_prompt(language: str, phone: str) -> str:
    return _localized(_VERIFY_PHONE_TEXTS, language).format(
        digits="-".join(phone)
    )


def invalid_phone_prompt(language: str) -> str:
    return _localized(_INVALID_PHONE_TEXTS, language)


def ask_issue_prompt(language: str) -> str:
    return _localized(_ASK_ISSUE_TEXTS, language)


def reask_issue_prompt(language: str) -> str:
    return _localized(_REASK_ISSUE_TEXTS, language)


def ask_name_prompt(language: str) -> str:
    return _localized(_ASK_NAME_TEXTS, language)


def reask_name_prompt(language: str) -> str:
    return _localized(_REASK_NAME_TEXTS, language)


def sanitize_customer_name(value: str) -> str:
    """Return a clean, blacklisted personal name or '' (never DOM noise).

    Strips layout labels (Footer/Header/Navigation/Main/…) and any token that
    isn't a plausible human name. Never returns raw widget text.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    # Normalize: collapse whitespace, strip outer punctuation / tags.
    candidate = re.sub(r"\s+", " ", raw).strip(" \t\n\r.,;:!?'\"<>[]()")
    candidate = re.sub(r"<[^>]+>", "", candidate).strip()
    if not candidate:
        return ""
    lower = candidate.lower()
    # Whole-string DOM labels are never names.
    if lower in _DOM_NAME_NOISE or candidate.lower() in {w.lower() for w in _DOM_NAME_NOISE}:
        return ""
    if _SKIP_NAME_RE.search(candidate):
        return ""
    if not _NAME_RE.match(candidate):
        return ""
    # Token-level: any single noise token poisons the whole capture.
    tokens = [t.strip("' .-") for t in candidate.split()]
    if any(t.lower() in _DOM_NAME_NOISE for t in tokens if t):
        return ""
    if any(t.lower() in _SENTENCE_NAME_TOKENS for t in tokens if t):
        return ""
    return candidate


def extract_customer_name(text: str) -> str:
    """Extract a name from an intro phrase ("my name is John") → sanitized."""
    m = _NAME_CAPTURE_RE.search(str(text or ""))
    if not m:
        return ""
    return sanitize_customer_name(m.group(2))


def ticket_created_with_issue_message(
    language: str,
    ticket_number: str,
    issue_summary: str,
    contact_kind: str = "",
    contact: str = "",
) -> str:
    """Confirmation echoing the ticket number, the customer's own issue summary
    and the callback channel/contact (e.g. we will call you at <phone> shortly)."""
    msg = _localized(_TICKET_CREATED_WITH_ISSUE_TEXTS, language)
    callback = ""
    if contact and contact_kind in _CALLBACK_PHRASES:
        callback = _localized(_CALLBACK_PHRASES[contact_kind], language).format(
            contact=contact
        )
    return msg.format(
        number=str(ticket_number or ""),
        summary=str(issue_summary or ""),
        callback=callback,
    )


def normalize_phone(phone: str) -> str:
    """Pure 10-15 digit string from any raw transcription (no separators)."""
    return normalize_phone_digits(phone)


# ── 2-Layer intent accumulation: user-statement buffer ────────────────────────
#
# Layer 1 of the engine. Every meaningful statement the customer makes while the
# intake FSM owns the conversation is buffered into
# `ticket_intake_pending["user_intents"]` (survives turn-to-turn via session
# meta). Names, phone-only replies, email addresses and read-back confirmations
# are excluded so the buffer only ever holds real intent signal — it never
# pollutes the persisted summary and is never fed to the LLM summarizer as-is.

# Whole-reply confirmations ("yes", "that's right", "theek hai"…) are never an
# intent — they are FSM answers, not issue descriptions.
_CONFIRMATION_RE = re.compile(
    r"^(?:yes|yeah|yep|correct|right|ok|okay|ha|haan|sahi|theek hai|thik hai|"
    r"sheri|sheriyano|sari|sariya|avun|correct ah|no|nope|nah|nahi|venda|nillu|"
    r"sare|fine|sure|alright|got it|that'?s (?:right|correct)|it'?s (?:right|correct)|"
    r"yes (?:please|correct|that'?s correct)|ok (?:yes|fine|sure)|okay (?:yes|fine))"
    r"[\s.,!?]*$",
    re.IGNORECASE,
)

# Contact-intro phrases ("my email is …", "reach me at …") — the value that
# follows is captured separately as contact info, the lead is not an intent.
_CONTACT_LEAD_RE = re.compile(
    r"(?i)\b(my email(?: address)? is|my phone(?: number)? is|my number is|"
    r"reach me at|contact me (?:at|on)|email me at)\b"
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RUN_RE = re.compile(r"\d[\d\s\-+()]{6,}")
_NAME_INTRO_RE = re.compile(r"(?i)\b(my name is|i am|i'?m|call me|this is|it'?s)\b")
# Conversational leads ("I have a …", "I got the …", "so …") add no signal and
# would otherwise land verbatim in the persisted summary.
_LEAD_STRIP_RE = re.compile(
    r"(?i)^\s*(?:i\s+(?:have|had|got)\s+(?:a\s+|an\s+|some\s+|the\s+)?|"
    r"there\s+(?:is|are)\s+(?:a\s+|some\s+)?|so\s+|well\s+|and\s+|actually\s+|"
    r"kindly\s+|please\s+)*"
)


def _extract_core_user_intent(text: str, customer_name: str = "") -> str:
    """Clean a user reply into the core intent statement, or '' when it's noise.

    Drops name-intro phrases ("my name is John"), the provided customer name,
    emails, phone-number runs, contact-intro leads, pure-confirmation replies and
    anything under 3 chars. A leftover single/short word that carries no specific
    issue signal ("Rahul", "my name is John") is treated as a name → ''.
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    s = raw
    s = _EMAIL_RE.sub(" ", s)
    s = _PHONE_RUN_RE.sub(" ", s)
    s = _CONTACT_LEAD_RE.sub(" ", s)
    s = _NAME_INTRO_RE.sub(" ", s)
    name = str(customer_name or "").strip()
    if name:
        s = re.sub(r"(?i)\b" + re.escape(name) + r"\b", " ", s)
    s = _LEAD_STRIP_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip(" \t\n\r.,;:!?'\"<>[]()")
    if not s:
        return ""
    if re.fullmatch(r"[\d\s\-+()]+", s):
        return ""
    if _CONFIRMATION_RE.fullmatch(s):
        return ""
    if len(s) < 3:
        return ""
    words = s.lower().split()
    # A leftover that reads like a name (1-2 words, no complaint signal) is not
    # an intent — keep only statements that carry real detail.
    if len(words) <= 2 and not any(w in _SPECIFIC_ISSUE_WORDS for w in words):
        return ""
    return s


def _buffer_user_intent(pending: Dict[str, Any], message: str) -> Dict[str, Any]:
    """Append the core intent of `message` to `pending["user_intents"]` (deduped,
    capped at the last 8) so it survives across intake turns. Returns `pending`
    unchanged when the message carries no intent."""
    intent = _extract_core_user_intent(message)
    if not intent:
        return pending
    intents = [str(i) for i in (pending.get("user_intents") or []) if str(i).strip()]
    if intent.lower() not in {i.lower() for i in intents}:
        intents.append(intent)
    pending = dict(pending)
    pending["user_intents"] = intents[-8:]
    return pending


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

    Flow (one field per voice prompt — human-like, step by step):
      AWAITING_CONTACT: ask for the 10-digit mobile number (or email).
      VERIFYING_PHONE: read the number back ("8-9-4-3-…") and require explicit
        "yes"/"correct" before moving on.
      AWAITING_NAME: ask "who do I have the pleasure of speaking with?" and
        sanitize the reply (DOM noise like Footer/Header/Navigation is dropped).
      AWAITING_ISSUE: ask what issue the team should help with; the customer's
        own words become the ticket's `issue_summary` and drive priority/heat.
      Nothing is persisted until the issue is described.
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
    state_now = (session_meta or {}).get("ticket_intake_state", TicketIntakeState.AWAITING_CONTACT)

    # Global cancel — EXCEPT during the name step, where "skip"/"guest"/"no
    # name" is a polite decline, not a ticket cancellation.
    if _CANCEL_RE.search(low) and not (
        state_now == TicketIntakeState.AWAITING_NAME and _SKIP_NAME_RE.search(low)
    ):
        logger.info("Ticket intake cancelled session=%s", session_id)
        return _localized(_CANCEL_TEXTS, language), TicketIntakeState.IDLE, {}, []

    # ── 2-Layer intent accumulation: buffer the customer's statements ────────
    # Seed the buffer with the escalation trigger once, then append every
    # meaningful reply (names / phones / confirmations are filtered inside
    # `_extract_core_user_intent`). The buffer lives in `ticket_intake_pending`
    # so it persists turn-to-turn; Layer 2 (`build_final_issue_summary`) joins a
    # generic final issue with the most recent specific detail from it.
    if not pending.get("user_intents") and pending.get("trigger_message"):
        pending = _buffer_user_intent(pending, str(pending.get("trigger_message") or ""))
    pending = _buffer_user_intent(pending, cleaned_message)

    # ── Verification step: awaiting "yes" on the read-back number ────────────
    if state_now == TicketIntakeState.VERIFYING_PHONE:
        pending_phone = str(pending.get("pending_phone") or "").strip()
        if _CONFIRM_RE.search(low) and pending_phone:
            logger.info(
                "Phone verified session=%s phone=%s (awaiting name)",
                session_id, pending_phone,
            )
            pending = dict(pending)
            pending["pending_phone"] = pending_phone
            return ask_name_prompt(language), TicketIntakeState.AWAITING_NAME, pending, []
        if _DENY_RE.search(low):
            # Number rejected — collect again.
            return reask_contact_prompt(language), TicketIntakeState.AWAITING_CONTACT, pending, []
        # Unclear reply — repeat the verification.
        return verify_phone_prompt(language, pending_phone), TicketIntakeState.VERIFYING_PHONE, pending, []

    # ── Name-capture step: ask, then sanitize (DOM noise never persisted) ────
    if state_now == TicketIntakeState.AWAITING_NAME:
        name = extract_customer_name(cleaned_message or "")
        if not name:
            name = sanitize_customer_name(cleaned_message or "")
        if not name:
            if _SKIP_NAME_RE.search(str(cleaned_message or "")):
                logger.info("Ticket name skipped session=%s", session_id)
                pending = dict(pending)
                pending["pending_name"] = ""
                return ask_issue_prompt(language), TicketIntakeState.AWAITING_ISSUE, pending, []
            logger.info("Ticket name rejected session=%s reply=%r", session_id, (cleaned_message or "")[:80])
            return reask_name_prompt(language), TicketIntakeState.AWAITING_NAME, pending, []
        logger.info("Ticket name captured session=%s name=%s", session_id, name)
        pending = dict(pending)
        pending["pending_name"] = name
        return ask_issue_prompt(language), TicketIntakeState.AWAITING_ISSUE, pending, []

    # ── Issue-capture step: the customer's own words become the ticket ────────
    if state_now == TicketIntakeState.AWAITING_ISSUE:
        issue = str(cleaned_message or "").strip()
        issue = re.sub(r"\s+", " ", issue).strip(" \t\n\r.,;:!?")
        if not issue or issue.lower() in _PII_PLACEHOLDERS or len(issue) < 3:
            return reask_issue_prompt(language), TicketIntakeState.AWAITING_ISSUE, pending, []
        logger.info("Ticket issue captured session=%s issue=%r", session_id, issue[:120])
        pending = dict(pending)
        pending["issue_summary"] = issue
        return await _create_ticket_with(
            pending=pending,
            language=language,
            tenant_id=tenant_id,
            session_id=session_id,
            conversation_history=conversation_history or [],
            store_client=store_client,
            session_service=session_service,
            customer_email=str(pending.get("pending_email") or ""),
            customer_phone=str(pending.get("pending_phone") or ""),
        )

    # ── Contact collection step ───────────────────────────────────────────────
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
        # A partial / mashed number (e.g. "73 72 27") must never be persisted
        # silently — tell the customer what went wrong and ask for a full one.
        digits = normalize_phone_digits(cleaned_message or "")
        if digits and len(digits) < 10:
            return (
                invalid_phone_prompt(language),
                TicketIntakeState.AWAITING_CONTACT,
                pending,
                [],
            )
        return reask_contact_prompt(language), TicketIntakeState.AWAITING_CONTACT, pending, []

    # An email is format-verified at capture — ask for the name, then issue.
    if email:
        logger.info("Ticket intake email session=%s email=%s (awaiting name)", session_id, email)
        pending = dict(pending)
        pending["pending_email"] = email
        return ask_name_prompt(language), TicketIntakeState.AWAITING_NAME, pending, []

    # Phone only — normalize, validate length, then read back for confirmation.
    clean_phone = normalize_phone(phone)
    if len(clean_phone) < 10:
        return invalid_phone_prompt(language), TicketIntakeState.AWAITING_CONTACT, pending, []

    pending = dict(pending)
    pending["pending_phone"] = clean_phone
    logger.info("Phone captured session=%s phone=%s (awaiting verification)", session_id, clean_phone)
    return verify_phone_prompt(language, clean_phone), TicketIntakeState.VERIFYING_PHONE, pending, []


async def _create_ticket_with(
    *,
    pending: Dict[str, Any],
    language: str,
    tenant_id: str,
    session_id: str,
    conversation_history: List[Dict[str, Any]],
    store_client: Any,
    session_service: Any,
    customer_email: str = "",
    customer_phone: str = "",
) -> Tuple[str, str, Dict[str, Any], List[Dict[str, Any]]]:
    """Create the ticket with the verified contact + user-stated issue → (text, next_state, pending, actions)."""
    ticket_result = await create_support_ticket(
        tenant_id=tenant_id,
        session_id=session_id,
        conversation_history=conversation_history,
        store_client=store_client,
        session_service=session_service,
        customer_email=customer_email,
        customer_phone=customer_phone,
        customer_name=str(pending.get("pending_name") or ""),
        trigger_message=str(pending.get("trigger_message") or ""),
        issue_summary=str(pending.get("issue_summary") or ""),
        user_intents=pending.get("user_intents"),
        product_id=pending.get("product_id"),
        source="deterministic",
    )

    actions: List[Dict[str, Any]] = []
    # Echo the FINAL issue summary (the 2-Layer engine's combined output), falling
    # back to the customer's raw statement when the result didn't carry one.
    issue_summary = str(
        ticket_result.get("issue_summary") or pending.get("issue_summary") or ""
    )
    contact_kind = "call" if customer_phone else ("email" if customer_email else "")
    contact = customer_phone or customer_email
    confirm_text = ticket_created_with_issue_message(
        language,
        str(ticket_result.get("ticket_number") or ""),
        issue_summary,
        contact_kind,
        contact,
    )
    if ticket_result.get("status") == "success":
        ticket_number = str(ticket_result.get("ticket_number") or "")
        actions.append({
            "type": "show_ticket",
            "payload": {
                "ticket_id": ticket_result["ticket_id"],
                "ticket_number": ticket_number,
                "priority": ticket_result.get("priority", "medium"),
                "heat": ticket_result.get("heat"),
                "message": confirm_text,
            },
        })
    text = str(ticket_result.get("message") or "") or confirm_text or ticket_created_message(language)
    return text, TicketIntakeState.IDLE, {}, actions
