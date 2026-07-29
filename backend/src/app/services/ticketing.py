import json
import logging
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

GORGIAS_WEBHOOK_URL = None


def _compile_transcript(conversation_history: List[Dict[str, Any]]) -> str:
    """Compile conversation history into a human-readable transcript string."""
    transcript_lines: List[str] = []
    for turn in conversation_history:
        role_raw = turn.get("role", "")
        content = turn.get("content", "")
        if role_raw == "user":
            label = "Shopper"
        elif role_raw == "assistant":
            label = "Assistant (Speako)"
        else:
            label = role_raw.capitalize()
        transcript_lines.append(f"{label}: {content}")
    return "\n".join(transcript_lines)


async def escalate_and_sync_shopify_ticket(
    customer_email: str,
    conversation_history: List[Dict[str, Any]],
    store_client: Any,
    shopify_customer_id: Optional[str] = None,
    gorgias_webhook_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Creates an external helpdesk ticket (Gorgias-compatible payload)
    and writes status trackers directly back to Shopify Customer Metafields.

    Args:
        customer_email: The customer's email address.
        conversation_history: Full list of conversation turns.
        store_client: The store client (ShopifyClient) for Admin mutation.
        shopify_customer_id: Shopify Customer GID (gid://shopify/Customer/...).
        gorgias_webhook_url: Override for Gorgias webhook endpoint.

    Returns:
        Dict with status and ticket_id.
    """
    ticket_id = f"SPEC-{uuid.uuid4().hex[:6].upper()}"

    formatted_transcript = _compile_transcript(conversation_history)

    gorgias_payload = {
        "customer": {"email": customer_email},
        "messages": [
            {
                "from": {"type": "customer", "email": customer_email},
                "body": "Conversation escalated from Speako Voice Assistant",
                "channel": "chat",
                "via": "api",
            },
            {
                "from": {"type": "assistant", "name": "Speako"},
                "body": formatted_transcript,
                "channel": "chat",
                "via": "api",
            },
        ],
        "channel": "chat",
        "status": "open",
        "subject": "Speako Voice Assistant Support Escalation",
        "via": "api",
        "external_id": ticket_id,
        "source": "speako-voice-assistant",
        "tags": ["speako", "voice-escalation"],
    }

    webhook_url = gorgias_webhook_url or GORGIAS_WEBHOOK_URL
    if webhook_url:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    webhook_url,
                    json=gorgias_payload,
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                logger.info(
                    "Gorgias ticket created: %s (status=%d)",
                    ticket_id, resp.status_code,
                )
        except Exception as exc:
            logger.warning("Failed to post ticket to Gorgias webhook: %s", exc)

    if shopify_customer_id and store_client:
        try:
            await store_client.update_customer_ticket_metafields(
                customer_id=shopify_customer_id,
                ticket_id=ticket_id,
                ticket_status="open",
                tags_to_add=["voice-support-open", "active-ticket"],
            )
            logger.info(
                "Shopify customer metafields updated for %s: ticket=%s",
                shopify_customer_id, ticket_id,
            )
        except Exception as exc:
            logger.warning("Failed to update Shopify customer metafields: %s", exc)

    return {"status": "success", "ticket_id": ticket_id}
