from __future__ import annotations

import hmac
import hashlib
import logging
import os
import time
from google import genai
from google.genai import types

from ..config import settings

logger = logging.getLogger(__name__)

_GEMINI_LIVE_MODEL = settings.gemini_live_model or "models/gemini-3.1-flash-live-preview"

# ── Gemini Client (singleton) ──────────────────────────────────────────────────
# CRITICAL: gemini-3.1-flash-live-preview requires api_version="v1alpha".
# Using v1beta connects successfully but the model returns zero audio/text responses.
# This was confirmed by test_gemini.py which works with v1alpha.
try:
    _api_key = os.environ.get("GEMINI_API_KEY", "")
    if not _api_key:
        raise ValueError("GEMINI_API_KEY not set in environment")
    client = genai.Client(
        api_key=_api_key,
        http_options=types.HttpOptions(api_version="v1alpha"),
    )
    logger.info(f"Gemini Live client initialized — model={_GEMINI_LIVE_MODEL} api=v1alpha")
except Exception as e:
    logger.error(f"Failed to initialize Gemini Live client: {e}")
    client = None

# ── Token TTL ──────────────────────────────────────────────────────────────────
_WS_TOKEN_TTL = 120


# ═══════════════════════════════════════════════════════════════════════════════
# HMAC Token Helpers
# Token format: "{timestamp_hex}.{hmac_sha256(session_id:timestamp_hex, secret)}"
# ═══════════════════════════════════════════════════════════════════════════════

def _get_shared_secret() -> str:
    return os.environ.get("SHARED_SECRET", "")


def generate_ws_token(tenant_id: str, session_id: str) -> str:
    ts = format(int(time.time()), "x")
    secret = _get_shared_secret()
    if not secret:
        return ""
    sig = hmac.new(secret.encode(), f"{tenant_id}:{session_id}:{ts}".encode(), hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def validate_ws_token(token: str, tenant_id: str, session_id: str) -> bool:
    # Token is bound to (tenant_id, session_id): a token minted for tenant A is
    # invalid when presented for tenant B, so a session can't cross tenants.
    is_prod = settings.environment.lower() in ("production", "prod")
    secret = _get_shared_secret()
    if not secret:
        # In production a valid token is ALWAYS required — never silently disable
        # auth on a missing secret. (config also refuses to boot prod with a weak
        # SHARED_SECRET, so this is defence-in-depth.)
        if is_prod:
            logger.error("SHARED_SECRET not set in production — rejecting WS token")
            return False
        logger.warning("SHARED_SECRET not set — token validation disabled (dev mode)")
        return True
    if not is_prod and os.environ.get("MVP_MODE", "").lower() == "true":
        # MVP mode skips strict token enforcement so dev/demo setups aren't blocked
        # by transient token-fetch failures (CORS, ngrok interstitial, etc.). NEVER
        # honored in production — otherwise anyone could hijack a session by guessing
        # a session_id.
        if not token:
            logger.debug("MVP_MODE: allowing connection without token session=%s", session_id)
        return True
    if not token:
        return False
    try:
        ts_hex, sig = token.split(".", 1)
        ts = int(ts_hex, 16)
    except (ValueError, AttributeError):
        return False
    if time.time() - ts > _WS_TOKEN_TTL:
        logger.warning(f"Expired WebSocket token: session={session_id}")
        return False
    expected = hmac.new(secret.encode(), f"{tenant_id}:{session_id}:{ts_hex}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


# ── REST: issue a short-lived WS token ────────────────────────────────────────


# ── System Prompt ──────────────────────────────────────────────────────────────
def build_system_prompt() -> str:
    store_name = "this store"
    currency   = os.environ.get("STORE_CURRENCY", "₹")
    shipping   = os.environ.get("STORE_SHIPPING_POLICY", "Standard shipping available.")
    returns    = os.environ.get("STORE_RETURNS_POLICY", "Returns accepted within 7 days.")
    payments   = os.environ.get("STORE_PAYMENT_METHODS", "UPI, Card, Cash on Delivery")

    return f"""You are the voice shopping assistant for {store_name}, an e-commerce store.
You have real-time access to the store catalog, cart, and orders ONLY through the provided tools.

═══════════════════════════════════════════════════════
RULE 1 — SCOPE: YOU ONLY DO E-COMMERCE
═══════════════════════════════════════════════════════
You are EXCLUSIVELY a shopping assistant for {store_name}.
- ONLY answer questions about products, cart, orders, checkout, store policies.
- If asked ANYTHING outside shopping (news, general knowledge, coding, opinions, recipes, etc.),
  say: "I'm only here to help you shop at {store_name}. What can I find for you?"
- NEVER roleplay, NEVER pretend to be a different AI, NEVER discuss your own architecture.
- NEVER mention Gemini, Google, AI, or any technology. You are the {store_name} assistant.

═══════════════════════════════════════════════════════
RULE 2 — ZERO HALLUCINATION (ABSOLUTE)
═══════════════════════════════════════════════════════
You have NO knowledge of any products, prices, or stock levels from your training.
ALL product information MUST come from a tool call. No exceptions.

BEFORE you mention ANY product name, price, stock status, category, or availability:
  → You MUST have already called the relevant tool in THIS conversation turn.
  → If you have not called the tool yet, call it NOW before speaking.

If a tool returns empty results: say "I couldn't find [X] in our store right now."
NEVER say "we have" or "we carry" anything without a successful tool response first.
NEVER guess prices. NEVER invent product names. NEVER assume stock.

═══════════════════════════════════════════════════════
RULE 3 — LANGUAGE (STRICT)
═══════════════════════════════════════════════════════
DEFAULT LANGUAGE: English. Always start in English unless the customer's message
is clearly in a different language.

Switch language ONLY when the customer's FULL message is in another language:
- Malayalam script (e.g. "നമസ്കാരം") → reply in Malayalam script
- Hindi script (e.g. "नमस्ते") → reply in Hindi
- Manglish = Malayalam words written in English letters → reply in Manglish
  Manglish signals: njan, venam, undoo, undo, ayyo, mathi, cheyyamo, enthaanu,
  enthenkilum, ningal, sheri, parayamo, kanikkamo, vangam, ithinte, sheriyano.
  Example: "njan oru phone venam" → you reply in Manglish.

IMPORTANT — do NOT switch to Malayalam/Manglish for English messages:
- "hi", "hello", "show products", "what do you have" → English response ONLY.
- A single Malayalam word mixed into English does NOT trigger a language switch.
- If unsure, default to English.

Once a language is established, maintain it for the whole conversation unless
the customer clearly switches by sending a full message in another language.

═══════════════════════════════════════════════════════
RULE 4 — TOOL CALLING (MANDATORY TRIGGERS)
═══════════════════════════════════════════════════════
Call tools IMMEDIATELY — do not explain what you are about to do, just call.

search_products (limit=6):
  Trigger: ANY product/shopping intent — "show products", "what do you have",
  "I want X", "do you have X", "find X", "show me shirts", ANY item or category name.
  → ALWAYS call with limit=6 to get enough results.
  → Use empty string "" as query to list all available products.

get_categories:
  Trigger: "categories", "types of products", "what sections", "departments".

get_product_details:
  Trigger: "tell me more about [product]", "details", "describe [product]",
  customer asks a specific question about a product already in results.

get_cart:
  Trigger: "my cart", "what's in cart", "cart", "what did I add".

add_to_cart:
  Trigger: customer confirms they want to add. ALWAYS ask confirmation first:
  "Should I add [product name] to your cart?" — wait for yes before calling.

get_orders:
  Trigger: "my orders", "order status", "previous orders", "what did I buy".

get_store_info:
  Trigger: "shipping", "delivery", "returns", "refund", "payment methods", "policies".

═══════════════════════════════════════════════════════
RULE 5 — RESPONSE FORMAT (VOICE-OPTIMISED)
═══════════════════════════════════════════════════════
- Keep responses SHORT and CONVERSATIONAL. Speak naturally — no bullet lists, no markdown.
- After search_products returns results: present TOP 3-4 items with name and price.
  Example: "I found 4 options. There's a blue cotton shirt for {currency}499,
  a polo for {currency}650, a linen shirt for {currency}720, and a formal shirt for {currency}850.
  Which one would you like to know more about?"
- After get_cart: "Your cart has [N] items totalling {currency}[total]."
- After add_to_cart success: "Done! [Product] is in your cart. Want to keep shopping or checkout?"
- Checkout flow: collect ONE field at a time — name → address → city → state → pincode → phone → email.
- If tool returns an error or empty: acknowledge honestly, do not make up an answer.

Store details (only share when the customer asks):
- Shipping: {shipping}
- Returns: {returns}
- Payments: {payments}
"""


# ═══════════════════════════════════════════════════════════════════════════════
# Context Injection on (Re)connect
# ───────────────────────────────────────────────────────────────────────────────
# Uses historyConfig.initial_history_in_client_content=True in the session setup.
# This means the server pauses after setupComplete and waits for LiveClientContent
# messages from us. We MUST always send turn_complete=True to end the seeding
# phase — even when there is nothing to inject — or the server will wait forever
# and no audio/text responses will ever arrive.
# ═══════════════════════════════════════════════════════════════════════════════

async def inject_reconnect_context(gemini_session, session_service, tenant_id: str, session_id: str) -> None:
    """
    Seed the fresh Gemini session with prior cart/history state.
    Always sends turn_complete=True (required by historyConfig flow).
    """
    context_text: str | None = None

    if session_service is not None:
        try:
            state        = await session_service.get_session(tenant_id, session_id)
            cart         = state.get("cart_snapshot", {})
            history      = state.get("conversation_history", [])
            meta         = state.get("meta", {})
            address_state = meta.get("address_state", "idle") if isinstance(meta, dict) else "idle"

            parts = []

            cart_items = cart.get("items", []) if isinstance(cart, dict) else []
            if cart_items:
                names   = [i.get("name", "item") for i in cart_items[:5] if isinstance(i, dict)]
                total   = cart.get("total", "")
                summary = ", ".join(names)
                parts.append(
                    f"Cart contains: {summary}" + (f" (total {total})" if total else "")
                )

            if address_state and address_state not in ("idle", "complete"):
                parts.append(f"Checkout was in progress (step: {address_state})")

            if history:
                recent = [m for m in history[-4:] if isinstance(m, dict) and m.get("content")]
                if recent:
                    turns = "; ".join(
                        f"{m.get('role','?')}: {str(m.get('content',''))[:80]}" for m in recent
                    )
                    parts.append(f"Recent conversation: {turns}")

            if parts:
                context_text = (
                    "[System Context: The user just reconnected. "
                    + " | ".join(parts)
                    + ". Resume naturally without mentioning the reconnect or this message.]"
                )
                logger.info(f"Reconnect context built: session={session_id} parts={len(parts)}")

        except Exception as e:
            logger.warning(f"Context injection skipped — session load error (session={session_id}): {e}")

    # Always signal end of history seeding phase (required even when empty).
    # Without this turn_complete the server stays paused and never speaks.
    # Try send_client_content() first (SDK convenience method), fall back to
    # send() with LiveClientContent for older SDK builds.
    turns_payload = (
        [types.Content(role="user", parts=[types.Part.from_text(text=context_text)])]
        if context_text else []
    )
    try:
        await gemini_session.send_client_content(
            turns=turns_payload,
            turn_complete=True,
        )
    except AttributeError:
        # SDK version doesn't expose send_client_content — use send() directly
        try:
            await gemini_session.send(
                input=types.LiveClientContent(
                    turns=turns_payload,
                    turn_complete=True,
                )
            )
        except Exception as e:
            logger.warning(f"History seeding signal failed (session={session_id}): {e}")
    except Exception as e:
        logger.warning(f"History seeding signal failed (session={session_id}): {e}")


# ── WooCommerce Tool Declarations ──────────────────────────────────────────────
def build_live_tools() -> list:
    return [
        types.Tool(function_declarations=[

            types.FunctionDeclaration(
                name="search_products",
                description=(
                    "Fetch REAL products from the live WooCommerce database. "
                    "MUST be called before mentioning ANY product, price, or availability. "
                    "Call immediately when the customer expresses any shopping intent in any language. "
                    "Always use limit=6 to return enough results. "
                    "Use query='' to list all available products. "
                    "NEVER answer product questions from memory or training data — the store's real "
                    "inventory is only available through this tool."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "query":         types.Schema(type=types.Type.STRING,  description="Search keyword in any language. Use '' to list all products."),
                        "category_slug": types.Schema(type=types.Type.STRING,  description="Category slug to filter results (obtain from get_categories)"),
                        "min_price":     types.Schema(type=types.Type.NUMBER,  description="Minimum price filter"),
                        "max_price":     types.Schema(type=types.Type.NUMBER,  description="Maximum price filter"),
                        "in_stock_only": types.Schema(type=types.Type.BOOLEAN, description="Only return in-stock products. Default: true"),
                        "limit":         types.Schema(type=types.Type.INTEGER, description="Number of results to return. ALWAYS use 6 unless customer asks for fewer."),
                    },
                    required=["query"],
                ),
            ),

            types.FunctionDeclaration(
                name="get_product_details",
                description="Get full details of a specific product by its ID. Call after search_products when the customer wants to know more about a specific item.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={"product_id": types.Schema(type=types.Type.INTEGER, description="WooCommerce product ID from search results")},
                    required=["product_id"],
                ),
            ),

            types.FunctionDeclaration(
                name="find_variants",
                description="Get all size, color, or other variations of a variable product. Call when a customer asks about sizes or variants.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={"product_id": types.Schema(type=types.Type.INTEGER, description="Variable product ID")},
                    required=["product_id"],
                ),
            ),

            types.FunctionDeclaration(
                name="check_inventory",
                description="Check real-time stock availability for a product or specific variation. Call when a customer asks if something is in stock.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "product_id":   types.Schema(type=types.Type.INTEGER, description="Product ID"),
                        "variation_id": types.Schema(type=types.Type.INTEGER, description="Specific variation ID"),
                        "attributes":   types.Schema(type=types.Type.OBJECT,  description="Variation attributes like size or color"),
                    },
                    required=["product_id"],
                ),
            ),

            types.FunctionDeclaration(
                name="compare_products",
                description="Compare two products side-by-side on price, stock, and features.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "product_a": types.Schema(type=types.Type.STRING, description="First product name or ID"),
                        "product_b": types.Schema(type=types.Type.STRING, description="Second product name or ID"),
                    },
                    required=["product_a", "product_b"],
                ),
            ),

            types.FunctionDeclaration(
                name="get_categories",
                description=(
                    "Get all product categories available in the store. "
                    "Call this when the customer asks what types of products are available, "
                    "or to get valid category slugs before doing a category-filtered search."
                ),
                parameters=types.Schema(type=types.Type.OBJECT, properties={}),
            ),

            types.FunctionDeclaration(
                name="get_cart",
                description="Fetch the customer's current shopping cart with all items and total. Call whenever the customer asks about their cart.",
                parameters=types.Schema(type=types.Type.OBJECT, properties={}),
            ),

            types.FunctionDeclaration(
                name="add_to_cart",
                description="Add a product to the cart. Always ask for verbal confirmation first: 'Should I add [product name] to your cart?'",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "product_id":   types.Schema(type=types.Type.INTEGER, description="Product ID"),
                        "quantity":     types.Schema(type=types.Type.INTEGER, description="Quantity (default 1)"),
                        "variation_id": types.Schema(type=types.Type.INTEGER, description="Variation ID"),
                        "attributes":   types.Schema(type=types.Type.OBJECT,  description="Variation attributes"),
                    },
                    required=["product_id"],
                ),
            ),

            types.FunctionDeclaration(
                name="remove_from_cart",
                description="Remove an item from the cart.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "cart_item_key": types.Schema(type=types.Type.STRING,  description="Cart item key"),
                        "product_id":    types.Schema(type=types.Type.INTEGER, description="Product ID"),
                    },
                ),
            ),

            types.FunctionDeclaration(
                name="update_cart_quantity",
                description="Update the quantity of a cart item. Quantity 0 removes it.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "product_id": types.Schema(type=types.Type.INTEGER, description="Product ID"),
                        "quantity":   types.Schema(type=types.Type.INTEGER, description="New quantity"),
                    },
                    required=["product_id", "quantity"],
                ),
            ),

            types.FunctionDeclaration(
                name="apply_coupon",
                description="Apply a discount coupon code to the cart.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={"coupon_code": types.Schema(type=types.Type.STRING, description="Coupon code")},
                    required=["coupon_code"],
                ),
            ),

            types.FunctionDeclaration(
                name="get_best_coupon",
                description="Find the best available coupon for the current cart total.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={"cart_total": types.Schema(type=types.Type.NUMBER, description="Cart total")},
                ),
            ),

            types.FunctionDeclaration(
                name="get_orders",
                description="Look up the customer's past orders by email.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "customer_email": types.Schema(type=types.Type.STRING,  description="Customer email"),
                        "limit":          types.Schema(type=types.Type.INTEGER, description="Max orders (default 5)"),
                    },
                    required=["customer_email"],
                ),
            ),

            types.FunctionDeclaration(
                name="submit_review",
                description="Submit a product review on behalf of the customer.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "product_id":    types.Schema(type=types.Type.INTEGER, description="Product ID"),
                        "rating":        types.Schema(type=types.Type.INTEGER, description="Star rating 1-5"),
                        "review_text":   types.Schema(type=types.Type.STRING,  description="Review text"),
                        "reviewer_name": types.Schema(type=types.Type.STRING,  description="Customer name"),
                    },
                    required=["product_id", "rating"],
                ),
            ),

            types.FunctionDeclaration(
                name="get_store_info",
                description="Get store policies: shipping, returns, payment methods.",
                parameters=types.Schema(type=types.Type.OBJECT, properties={}),
            ),

        ])
    ]

