"""
Support-ticket orchestration for the agent.

Called by the LLM tools `create_support_ticket` / `request_human_support` when a
customer needs human help (refunds, damaged orders, unresolvable catalog queries,
or an explicit request to talk to a human).

What it does:
  1. Persists the ticket to the `voice_tickets` table (merchant-scoped, RLS-protected)
  2. Emits a `ticket.created` webhook to the merchant's external helpdesk (async)
  3. Best-effort syncs a "speako" ticket tag + metafields onto the Shopify customer
"""
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

TICKET_NAMESPACE = "speako"
TICKET_STATUS_KEY = "ticket_status"
TICKET_ID_KEY = "last_ticket_id"

# The exact spoken line the LLM must use when a ticket is created.
TICKET_CREATED_MESSAGE = (
    "I've created a support ticket for your request. "
    "Our team will review your chat history and contact you shortly."
)
# Localized variants for the deterministic escalation path (voice speaks these
# directly without the LLM, so they must match the detected language).
_TICKET_CREATED_LOCALIZED = {
    "en": TICKET_CREATED_MESSAGE,
    "hi": "Maine aapki request ke liye ek support ticket bana di hai. Hamari team aapki chat history dekh kar jald hi contact karegi.",
    "ml": "Ningalude appeal-inaayi njaan oru support ticket undaakkiyittund. Njangalude team ningalude chat history parichayicchu thazhottu bandhappetum.",
    "ta": "Ungal kavalai-kkaga support ticket uraakkiyirukkirrom. Engal team ungal chat-ai parthu sathanaikku ungalai thodarpaduven.",
    "te": "Mee vinathi kosam support ticket tayaru chesanu. Maa team mee chat history chusi tvaralo kontact chestundi.",
    "bn": "Apanr anurodher jonno ami ekta support ticket tairi korechi. Amaader team apnar chat history dekhbe ebong sate-sate yogo jog korbe.",
    "kn": "Nimma vinathi-kkagi nanu support ticket madidde. Namma team nimma chat history nodi bega samparkisuttade.",
}

# Tickets are de-duplicated within this window: a repeated complaint in the SAME
# session reuses the open ticket instead of inserting a duplicate row.
DEDUP_WINDOW_MINUTES = 60

# ── Priority heuristics (keyword-based, deterministic — no extra LLM latency) ─

_URGENT_TOKENS = {
    "urgent", "asap", "immediately", "right now", "emergency", "fire",
    "furious", "angry", "very upset", "manager", "complain", "complaint",
    "legal", "sue", "lawsuit", "scam", "fraud", "stolen", "bleeding",
    "waiting too long", "unacceptable", "worst", "terrible service",
}
_HIGH_PRIORITY_TOKENS = {
    "refund", "refunds", "damaged", "damage", "broken", "defective", "wrong item",
    "wrong item received", "missing item", "never received", "not received",
    "urgent", "asap", "furious", "angry", "very upset", "manager", "complain",
    "complaint", "legal", "sue", "lawsuit", "scam", "fraud", "not delivered",
    "didn't receive", "exchanged", "return", "returning", "returned", "cracked",
    "torn", "stolen",
}
_LOW_PRIORITY_TOKENS = {
    "talk to human", "talk to a human", "human agent", "representative", "customer care",
    "talk to someone", "speak to someone", "real person", "agent please", "helpdesk",
    "contact support", "talk to support", "speak to support",
}

# Triage heat tiers — how quickly a human must jump in. Derived from priority +
# escalation keywords so the dashboard can sort by urgency.
_HOT_TOKENS = _URGENT_TOKENS
_WARM_TOKENS = {
    "refund", "damaged", "damage", "broken", "defective", "wrong item",
    "missing item", "never received", "not received", "not delivered", "cracked",
    "torn", "return", "returning", "returned", "exchange", "exchanged",
}


def _detect_priority(text: str) -> str:
    low = " ".join(str(text or "").lower().split())
    if any(tok in low for tok in _URGENT_TOKENS):
        return "urgent"
    if any(tok in low for tok in _HIGH_PRIORITY_TOKENS):
        return "high"
    if any(tok in low for tok in _LOW_PRIORITY_TOKENS):
        return "low"
    return "medium"


def _detect_heat(text: str, priority: str) -> str:
    """Map text+priority onto a triage heat tier: hot / warm / cold."""
    low = " ".join(str(text or "").lower().split())
    if priority == "urgent":
        return "hot"
    if any(tok in low for tok in _HOT_TOKENS):
        return "hot"
    if priority in ("high",) or any(tok in low for tok in _WARM_TOKENS):
        return "warm"
    return "cold"


def ticket_created_message(language: str = "en", ticket_number: str = "") -> str:
    """Language-aware ticket-confirmation line for the deterministic path."""
    msg = _TICKET_CREATED_LOCALIZED.get(language, _TICKET_CREATED_LOCALIZED["en"])
    if ticket_number:
        return f"{msg} Your ticket number is {ticket_number}."
    return msg


def _classify_issue(text: str) -> str:
    """Map free text onto a structured issue_type for dashboard filtering."""
    low = str(text or "").lower()
    if not low:
        return "other"

    def _has(*words: str) -> bool:
        return any(w in low for w in words)

    if _has("damaged", "damage", "broken", "defective", "cracked", "torn"):
        return "damaged_order"
    if _has("wrong item", "wrong product", "wrong items", "incorrect item", "received the wrong"):
        return "wrong_item"
    if _has(
        "missing item", "missing items", "missing part", "items missing", "item missing",
        "missing order", "lost order", "missing",
        "didn't receive", "did not receive", "never received", "not received",
        "not delivered", "never arrived", "didn't arrive",
    ):
        return "missing_item"
    if _has("refund"):
        return "refund"
    if _has("exchange"):
        return "exchange"
    if _has("delay", "delayed", "late delivery", "delivery delay", "shipping delay", "stuck"):
        return "delivery_issue"
    if _has("charged", "charge", "billing", "double charge", "overcharged", "extra charge"):
        return "billing"
    if _has(
        "talk to human", "talk to a human", "human agent", "representative", "customer care",
        "talk to someone", "speak to someone", "real person", "manager", "helpdesk",
        "contact support",
    ):
        return "talk_to_human"
    return "other"


def _extract_order_id(text: str) -> str:
    """Best-effort order-number extraction ("order #1234", "#ABCD123", "order 1234")."""
    low = str(text or "").strip()
    if not low:
        return ""
    # Explicit marker: "order #1234", "order number: 1234", "tracking id 1234".
    m = re.search(
        r"\b(?:order|booking|tracking|shipment)\s*(?:id|number|no\.?|#)?\s*[#:]\s*"
        r"([A-Za-z0-9][A-Za-z0-9-]{3,19})",
        low,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip().upper()[:100]
    # Bare "#1234" anywhere in the message.
    m = re.search(r"\b#([A-Za-z0-9][A-Za-z0-9-]{3,19})\b", low)
    if m:
        return m.group(1).strip().upper()[:100]
    # Bare digits after an order word: "order 99887766" / "order no 1234". Digits
    # only — never a word ("order damage aayirunnu" must NOT match).
    m = re.search(
        r"\b(?:order|booking|tracking|shipment)\s+(?:(?:number|no\.?|id)\s+)?(\d{4,14})\b",
        low, re.IGNORECASE,
    )
    if m:
        return m.group(1).strip().upper()[:100]
    return ""


def _priority_reason(text: str) -> str:
    """Which keyword drove the priority, or 'llm' when none did."""
    low = " ".join(str(text or "").lower().split())
    for tok in _URGENT_TOKENS:
        if tok in low:
            return f"keyword:{tok}"
    for tok in _HIGH_PRIORITY_TOKENS:
        if tok in low:
            return f"keyword:{tok}"
    for tok in _LOW_PRIORITY_TOKENS:
        if tok in low:
            return f"keyword:{tok}"
    return "llm"


def _build_transcript_turns(conversation_history: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Conversation history → [{role, content}] turns (sanitized + bounded)."""
    turns: List[Dict[str, str]] = []
    for turn in (conversation_history or [])[-40:]:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role", "")).strip().lower()
        content = str(turn.get("content", "")).strip()
        if role in {"user", "assistant"} and content:
            turns.append({"role": role, "content": content[:1000]})
    return turns


def _build_transcript_text(conversation_history: List[Dict[str, Any]]) -> str:
    """Human-readable transcript for the legacy helpdesk payloads."""
    lines: List[str] = []
    for turn in _build_transcript_turns(conversation_history):
        label = "Shopper" if turn["role"] == "user" else "Assistant (Speako)"
        lines.append(f"{label}: {turn['content']}")
    return "\n".join(lines)


def _generate_issue_summary(
    conversation_history: List[Dict[str, Any]],
    trigger_message: str,
) -> str:
    """Deterministic AI-style summary: the customer's recent turns + trigger tag."""
    user_turns = [
        str(t.get("content", "")).strip()
        for t in (conversation_history or [])
        if isinstance(t, dict) and str(t.get("role", "")).lower() == "user"
    ]
    if trigger_message and trigger_message.strip():
        user_turns.append(str(trigger_message).strip())
    recent = " ".join(user_turns[-3:])
    recent = re.sub(r"\s+", " ", recent)[:500]
    if "refund" in (recent or "").lower():
        summary = "Customer is requesting a refund."
    elif any(w in (recent or "").lower() for w in ("damaged", "broken", "defective", "cracked", "torn")):
        summary = "Customer reports receiving a damaged/defective order."
    else:
        summary = "Customer requested human assistance / could not be resolved by the assistant."
    if recent:
        summary += f" Context: {recent}"
    return summary[:600]


def _resolve_shop_domain(store_client: Any, shop_domain: Optional[str]) -> str:
    if shop_domain and str(shop_domain).strip():
        return str(shop_domain).strip().rstrip("/")
    if store_client is None:
        return ""
    domain = (
        getattr(store_client, "store_domain", "")
        or getattr(store_client, "base_url", "")
        or ""
    )
    return str(domain).strip().rstrip("/")


# ── Customer context from the active session / address FSM ────────────────────

async def _collect_customer_context(
    session_service: Any,
    tenant_id: str,
    session_id: str,
    customer_email: str,
) -> Dict[str, str]:
    """Pull name/phone/email from session meta (address_data FSM + customer_email)."""
    context: Dict[str, str] = {
        "email": str(customer_email or "").strip().lower(),
        "phone": "",
        "name": "",
    }
    if session_service is None:
        return context
    try:
        meta = await session_service.get_meta(tenant_id, session_id)
        if not isinstance(meta, dict):
            meta = {}
        if not context["email"]:
            context["email"] = str(meta.get("customer_email") or "").strip().lower()
        name = str(meta.get("customer_name") or "").strip()
        if not name:
            state = await session_service.get_session(tenant_id, session_id)
            state = state if isinstance(state, dict) else {}
            name = str((state.get("meta") or {}).get("customer_name") or "").strip()
        context["name"] = name
        addr = meta.get("address_data")
        if isinstance(addr, dict):
            context["phone"] = str(addr.get("phone") or "").strip()
            if not context["email"]:
                context["email"] = str(addr.get("email") or "").strip().lower()
    except Exception as exc:
        logger.debug("Customer context collection failed: %s", exc)
    return context


# ── Primary entry point ────────────────────────────────────────────────────────

async def create_support_ticket(
    *,
    tenant_id: str,
    session_id: str,
    conversation_history: List[Dict[str, Any]],
    store_client: Any,
    session_service: Any = None,
    customer_email: str = "",
    customer_phone: str = "",
    customer_name: str = "",
    shop_domain: Optional[str] = None,
    trigger_message: str = "",
    product_id: Optional[str] = None,
    source: str = "llm",
) -> Dict[str, Any]:
    """Create a voice ticket, persist it, emit the helpdesk webhook, and sync
    Shopify customer metafields. Returns the ticket id + the message to speak.

    De-duplication: an OPEN ticket for the same tenant+session created within
    the last `DEDUP_WINDOW_MINUTES` is reused (already_exists=true, no
    insert/webhook/sync) so a customer repeating their complaint in one session
    does not create duplicate rows. Only active when `session_id` is present.
    """
    # ── Dedup: reuse an OPEN ticket for this tenant+session within 60 min ─────
    ticket_id: Optional[str] = None
    ticket_number: Optional[str] = None
    heat: Optional[str] = None
    already_exists = False
    priority = "medium"
    issue_summary = ""
    if session_id and str(session_id).strip() and str(session_id).strip() != "legacy":
        try:
            from ..core.database import AsyncSessionLocal
            from ..modules.tickets.repository import TicketRepository

            since = datetime.now(timezone.utc) - timedelta(minutes=DEDUP_WINDOW_MINUTES)
            async with AsyncSessionLocal() as db:
                existing = await TicketRepository(db).find_open_by_session(
                    tenant_id, str(session_id).strip(), since
                )
                if existing:
                    ticket_id = existing.id
                    already_exists = True
                    priority = existing.priority
                    issue_summary = existing.issue_summary
                    ticket_number = existing.ticket_number
                    heat = existing.heat
                    logger.info(
                        "Ticket dedup: reusing open ticket %s (session %s, <%dmin)",
                        ticket_id, session_id, DEDUP_WINDOW_MINUTES,
                    )
        except Exception as exc:
            logger.debug("Ticket dedup lookup failed session=%s: %s", session_id, exc)

    if already_exists:
        return {
            "status": "success",
            "ticket_id": ticket_id,
            "ticket_number": ticket_number,
            "heat": heat,
            "already_exists": True,
            "priority": priority,
            "issue_summary": issue_summary,
            "transcript": _build_transcript_text(conversation_history),
            "message": ticket_created_message("en", ticket_number or ""),
            "spoken_message": ticket_created_message("en", ticket_number or ""),
        }

    customer = await _collect_customer_context(
        session_service, tenant_id, session_id, customer_email
    )
    phone = str(customer_phone or customer["phone"] or "").strip()
    name = str(customer_name or customer["name"] or "").strip()
    email = str(customer_email or customer["email"] or "").strip().lower()

    transcript_turns = _build_transcript_turns(conversation_history)
    context_text = " ".join(
        t["content"] for t in transcript_turns if t["role"] == "user"
    )
    priority = _detect_priority(trigger_message or context_text)
    issue_summary = _generate_issue_summary(conversation_history, trigger_message)
    issue_type = _classify_issue(trigger_message or context_text)
    order_id = _extract_order_id(trigger_message or context_text)
    priority_reason = _priority_reason(trigger_message or context_text)
    heat = _detect_heat(trigger_message or context_text, priority)

    try:
        from ..core.database import AsyncSessionLocal
        from ..modules.tickets.service import TicketService

        async with AsyncSessionLocal() as db:
            ticket = await TicketService(db).create_ticket(
                tenant_id,
                {
                    "shop_domain": _resolve_shop_domain(store_client, shop_domain) or None,
                    "session_id": session_id,
                    "customer_name": name or None,
                    "customer_phone": phone or None,
                    "customer_email": email or None,
                    "issue_summary": issue_summary,
                    "transcript_json": {"turns": transcript_turns},
                    "priority": priority,
                    "status": "open",
                    "issue_type": issue_type,
                    "order_id": order_id or None,
                    "product_id": product_id or None,
                    "priority_reason": priority_reason,
                    "source": source or "llm",
                    "heat": heat,
                },
            )
            ticket_id = ticket.id
            ticket_number = ticket.ticket_number
    except Exception as exc:
        # Persistence must never take down the voice turn — degrade to an
        # in-memory ticket id so the customer still gets the confirmation.
        logger.error("Ticket persistence failed session=%s: %s", session_id, exc, exc_info=True)
        ticket_id = f"SPEC-{uuid.uuid4().hex[:6].upper()}"
        ticket_number = None

    transcript_text = _build_transcript_text(conversation_history)

    # Best-effort: tag + metafield the Shopify customer so the merchant sees the
    # escalation in Shopify Admin. Never blocks or fails the turn.
    try:
        await _sync_shopify_customer(store_client, email, ticket_id)
    except Exception as exc:
        logger.warning("Shopify ticket metafield sync skipped: %s", exc)

    confirm_msg = ticket_created_message("en", ticket_number or "")
    return {
        "status": "success",
        "ticket_id": ticket_id,
        "ticket_number": ticket_number,
        "heat": heat,
        "priority": priority,
        "issue_summary": issue_summary,
        "transcript": transcript_text,
        "message": confirm_msg,
        "spoken_message": confirm_msg,
    }


# ── Shopify customer sync (legacy, best-effort) ────────────────────────────────

async def _resolve_shopify_customer_id(store_client: Any, customer_email: str) -> Optional[str]:
    """Look up Shopify Customer GID by email via Admin API."""
    if not store_client or not customer_email:
        return None
    try:
        gql = """
        query GetCustomerByEmail($email: String!) {
          customers(first: 1, query: "email:%s") {
            edges { node { id } }
          }
        }
        """ % customer_email
        data = await store_client._admin_graphql(gql)
        edges = data.get("customers", {}).get("edges", [])
        if edges:
            return edges[0]["node"]["id"]
    except Exception as exc:
        logger.warning("Could not resolve Shopify customer ID for %s: %s", customer_email, exc)
    return None


async def _sync_shopify_customer(
    store_client: Any,
    customer_email: str,
    ticket_id: str,
    shopify_customer_id: Optional[str] = None,
) -> None:
    """Write a voice-support tag + ticket metafields onto the Shopify customer."""
    if not store_client:
        return
    resolved_id: Optional[str] = shopify_customer_id
    if not resolved_id and customer_email:
        resolved_id = await _resolve_shopify_customer_id(store_client, customer_email)
    if not resolved_id:
        return
    try:
        existing_tags = await _get_customer_tags(store_client, resolved_id)
        all_tags = list(set(existing_tags + ["voice-support-open", "escalated-from-speako"]))

        gql = """
        mutation updateCustomerMetafields($input: CustomerInput!) {
          customerUpdate(input: $input) {
            customer { id tags }
            userErrors { field message }
          }
        }
        """
        variables = {
            "input": {
                "id": resolved_id,
                "tags": all_tags,
                "metafields": [
                    {
                        "namespace": TICKET_NAMESPACE,
                        "key": TICKET_ID_KEY,
                        "value": ticket_id,
                        "type": "single_line_text_field",
                    },
                    {
                        "namespace": TICKET_NAMESPACE,
                        "key": TICKET_STATUS_KEY,
                        "value": "open",
                        "type": "single_line_text_field",
                    },
                ],
            }
        }
        data = await store_client._admin_graphql(gql, variables)
        errors = data.get("customerUpdate", {}).get("userErrors", [])
        if errors:
            logger.warning("Shopify customer metafield update errors: %s", errors)
        else:
            logger.info(
                "Wrote ticket %s metafields to Shopify customer %s",
                ticket_id, resolved_id,
            )
    except Exception as exc:
        logger.warning("Shopify metafield write failed for ticket %s: %s", ticket_id, exc)


async def _get_customer_tags(store_client: Any, customer_gid: str) -> List[str]:
    """Fetch existing tags from a Shopify customer profile."""
    try:
        gql = """
        query GetCustomerTags($id: ID!) {
          customer(id: $id) {
            tags
          }
        }
        """
        data = await store_client._admin_graphql(gql, {"id": customer_gid})
        raw = data.get("customer", {}).get("tags")
        if isinstance(raw, list):
            return [str(t) for t in raw]
        if isinstance(raw, str):
            return [t.strip() for t in raw.split(",") if t.strip()]
    except Exception:
        pass
    return []


# ── Backwards-compatible alias ─────────────────────────────────────────────────

async def escalate_and_sync_shopify_ticket(
    customer_email: str,
    conversation_history: List[Dict[str, Any]],
    store_client: Any,
    shopify_customer_id: Optional[str] = None,
    *,
    tenant_id: str = "_dev",
    session_id: str = "",
    session_service: Any = None,
    shop_domain: Optional[str] = None,
) -> Dict[str, Any]:
    """Legacy entry point — now persists a real voice_tickets row too.

    Kept for callers in agent/tools/base.py and anything else still invoking it.
    """
    return await create_support_ticket(
        tenant_id=tenant_id,
        session_id=session_id or "legacy",
        conversation_history=conversation_history,
        store_client=store_client,
        session_service=session_service,
        customer_email=customer_email,
        shop_domain=shop_domain,
    )
