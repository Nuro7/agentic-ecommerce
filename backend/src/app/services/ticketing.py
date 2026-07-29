import os
import uuid
import json
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

TICKET_NAMESPACE = "speako"
TICKET_STATUS_KEY = "ticket_status"
TICKET_ID_KEY = "last_ticket_id"


async def _resolve_shopify_customer_id(
    store_client: Any,
    customer_email: str,
) -> Optional[str]:
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


def _build_transcript(conversation_history: List[Dict[str, Any]]) -> str:
    """Format conversation_history into a human-readable chat transcript."""
    lines: List[str] = []
    for turn in conversation_history:
        role = turn.get("role", "unknown")
        content = turn.get("content", "")
        label = "Shopper" if role == "user" else "Assistant (Speako)"
        lines.append(f"{label}: {content}")
    return "\n".join(lines)


async def escalate_and_sync_shopify_ticket(
    customer_email: str,
    conversation_history: List[Dict[str, Any]],
    store_client: Any,
    shopify_customer_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a helpdesk ticket (Gorgias-compatible), return ticket ID,
    and write status metafields to the Shopify customer profile.
    """
    ticket_id = f"SPEC-{uuid.uuid4().hex[:6].upper()}"
    transcript = _build_transcript(conversation_history)

    gorgias_webhook = os.getenv("GORGIAS_WEBHOOK_URL", "").strip()
    if gorgias_webhook:
        gorgias_payload = {
            "customer": {"email": customer_email},
            "messages": [
                {
                    "text": transcript,
                    "channel": "chat",
                    "source": "api",
                }
            ],
            "channel": "chat",
            "status": "open",
            "subject": "Speako Voice Assistant Support Escalation",
            "via": "api",
            "external_id": ticket_id,
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    gorgias_webhook,
                    json=gorgias_payload,
                    headers={"Content-Type": "application/json"},
                )
                if resp.is_success:
                    logger.info("Gorgias ticket %s created successfully", ticket_id)
                else:
                    logger.warning(
                        "Gorgias webhook returned %s for ticket %s",
                        resp.status_code, ticket_id,
                    )
        except Exception as exc:
            logger.warning("Gorgias webhook post failed for ticket %s: %s", ticket_id, exc)

    resolved_id: Optional[str] = shopify_customer_id
    if not resolved_id and customer_email:
        resolved_id = await _resolve_shopify_customer_id(store_client, customer_email)

    if resolved_id and store_client:
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

    return {"status": "success", "ticket_id": ticket_id, "transcript": transcript}


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
