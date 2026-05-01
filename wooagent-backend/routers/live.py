import asyncio
import os
import json
import hmac
import hashlib
import time
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from google import genai
from google.genai import types

# ═══════════════════════════════════════════════════════════════════════════════
# NEW ARCHITECTURE: Gemini 3.1 Flash Live A2A (Audio-to-Audio) Relay
# ───────────────────────────────────────────────────────────────────────────────
# Model: gemini-3.1-flash-live-preview
#
# A2A flow (WebSocket, low-latency, 1 connection):
#   Browser ↔ WebSocket /wooagent/stream ↔ Python relay ↔ Gemini 3.1 Live API
#   ├── Browser streams raw PCM Int16 16kHz audio via AudioWorklet
#   ├── Relay pipes audio via send_realtime_input(audio=...) — no STT step
#   ├── Gemini 3.1 handles STT + reasoning + TTS natively
#   ├── Text messages during conversation → send_realtime_input(text=...)
#   ├── Relay pipes Gemini PCM audio bytes back to browser
#   ├── A single ServerContent event may carry BOTH audio + transcript parts
#   ├── Tool calls → synchronous WooCommerce REST execution (async NOT supported in 3.1)
#   ├── Barge-in → flush_audio signal to browser
#   └── WebSocket tokens (HMAC-signed, 120s TTL) prevent unauthorized connections
#
# API version notes (tested & confirmed):
#   • gemini-3.1-flash-live-preview REQUIRES api_version="v1alpha"
#     (v1beta silently connects but produces zero responses)
#   • thinkingBudget removed → thinkingLevel used (minimal/low/medium/high)
#     Must be set inside LiveConnectConfig constructor, NOT as a post-assignment
#   • In-conversation text → send_realtime_input(text=...), NOT send_client_content
#   • send_client_content / LiveClientContent is for initial history seeding only
#     (requires historyConfig.initial_history_in_client_content=True in setup)
#   • Async function calling NOT supported in 3.1 → all tool calls are synchronous
#   • A single ServerContent event can contain multiple parts simultaneously
# ═══════════════════════════════════════════════════════════════════════════════

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Model & API version ────────────────────────────────────────────────────────
_GEMINI_LIVE_MODEL = "models/gemini-3.1-flash-live-preview"

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


def _generate_ws_token(session_id: str) -> str:
    ts = format(int(time.time()), "x")
    secret = _get_shared_secret()
    if not secret:
        return ""
    sig = hmac.new(secret.encode(), f"{session_id}:{ts}".encode(), hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def _validate_ws_token(token: str, session_id: str) -> bool:
    secret = _get_shared_secret()
    if not secret:
        logger.warning("SHARED_SECRET not set — token validation disabled (dev mode)")
        return True
    if os.environ.get("MVP_MODE", "").lower() == "true":
        # In MVP mode skip strict token enforcement so dev/demo setups aren't blocked
        # by transient token-fetch failures (CORS, ngrok interstitial, etc.)
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
    expected = hmac.new(secret.encode(), f"{session_id}:{ts_hex}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


# ── REST: issue a short-lived WS token ────────────────────────────────────────
@router.get("/wooagent/ws-token")
async def get_ws_token(session_id: str = Query(..., min_length=4, max_length=128)):
    token = _generate_ws_token(session_id)
    return {"token": token, "ttl": _WS_TOKEN_TTL}


# ── System Prompt ──────────────────────────────────────────────────────────────
def _build_system_prompt() -> str:
    store_name = os.environ.get("STORE_NAME", "the store")
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

async def _inject_reconnect_context(gemini_session, session_service, session_id: str) -> None:
    """
    Seed the fresh Gemini session with prior cart/history state.
    Always sends turn_complete=True (required by historyConfig flow).
    """
    context_text: str | None = None

    if session_service is not None:
        try:
            state        = await session_service.get_session(session_id)
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
def _build_woocommerce_tools() -> list:
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


# ═══════════════════════════════════════════════════════════════════════════════
# A2A WebSocket Relay
# ═══════════════════════════════════════════════════════════════════════════════

@router.websocket("/wooagent/stream")
async def gemini_live_relay(websocket: WebSocket):
    """
    Gemini 3.1 Flash Live A2A relay.

    Browser → backend (binary):  raw PCM Int16 16kHz mono from AudioWorklet
    Browser → backend (text):    {"type":"text_input","text":"..."}

    Backend → browser (binary):  PCM 16-bit 24kHz mono from Gemini TTS
    Backend → browser (text):    {"type":"transcript","text":"..."}
                                 {"type":"ui_action","action":{...}}
                                 {"type":"flush_audio"}  — barge-in clear
    """
    session_id = websocket.query_params.get("session_id", "anonymous")
    token      = websocket.query_params.get("token", "")

    if not _validate_ws_token(token, session_id):
        await websocket.close(code=4003, reason="Invalid or expired token")
        logger.warning(f"WebSocket rejected — bad token: session={session_id}")
        return

    await websocket.accept()

    if client is None:
        logger.error("Gemini client not initialized — check GEMINI_API_KEY")
        await websocket.close(code=1011, reason="Gemini client unavailable")
        return

    woo_client      = getattr(websocket.app.state, "woo_client",      None)
    session_service = getattr(websocket.app.state, "session_service", None)

    # ── Connection-time diagnostics ───────────────────────────────────────────
    # These lines tell you immediately why tools might not work.
    store_url = getattr(getattr(woo_client, "wc", woo_client), "base_url", "") or ""
    if not store_url:
        logger.error(
            f"WOOCOMMERCE_STORE_URL is empty — all tool calls will return no data. "
            f"Set WOOCOMMERCE_STORE_URL in .env to your WordPress site URL. (session={session_id})"
        )
    else:
        logger.info(f"WooCommerce store: {store_url[:60]} (session={session_id})")

    if woo_client is None:
        logger.error(f"WooCommerce client is None — tool calls disabled (session={session_id})")

    tools_declared = len(_build_woocommerce_tools()[0].function_declarations) if woo_client else 0
    logger.info(f"A2A stream connected: session={session_id} tools_declared={tools_declared}")

    try:
        # ── Session Config ────────────────────────────────────────────────────
        # IMPORTANT: thinking_config MUST be set inside the constructor.
        # Post-assignment (live_config.thinking_config = ...) is silently ignored
        # by the SDK serializer and the model receives no thinking config at all.
        #
        # history_config.initial_history_in_client_content=True:
        # Server pauses after setupComplete, waits for send_client_content calls,
        # then resumes realtime mode after turn_complete=True. We MUST always
        # send that signal (see _inject_reconnect_context) or the model never speaks.
        # ── Voice selection ───────────────────────────────────────────────────
        # MULTILINGUAL voices (support 70+ languages natively):
        #   Aoede, Charon, Fenrir, Kore, Leda, Orus, Sulafat, Zephyr
        # ENGLISH-ONLY voice (do NOT use for multilingual stores):
        #   Puck
        # Override via GEMINI_VOICE env var if you want a different voice.
        voice_name = os.environ.get("GEMINI_VOICE", "Aoede")

        live_config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=types.Content(
                parts=[types.Part.from_text(text=_build_system_prompt())]
            ),
            tools=_build_woocommerce_tools() if woo_client else [],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=voice_name
                    )
                )
                # Do NOT set language_code here — leave it unset so Gemini
                # auto-detects the customer's language from their speech.
                # Setting a fixed language_code locks the model to that language
                # and breaks multilingual detection.
            ),
            # Input transcription: get the customer's words as text in their language
            input_audio_transcription=types.AudioTranscriptionConfig(),
            # Output transcription: get the assistant's response text for display
            output_audio_transcription=types.AudioTranscriptionConfig(),
            thinking_config=types.ThinkingConfig(thinking_level="minimal"),
            history_config=types.HistoryConfig(
                initial_history_in_client_content=True
            ),
        )

        async with client.aio.live.connect(
            model=_GEMINI_LIVE_MODEL,
            config=live_config,
        ) as gemini_session:

            logger.info(f"Gemini Live session open: session={session_id}")

            # Inject session context BEFORE starting dual tasks.
            # This is synchronous (awaited) so audio relay only starts after
            # the history seeding phase is complete and the server is in
            # realtime mode. Concurrent injection caused a race where audio
            # arrived before the server unblocked.
            await _inject_reconnect_context(gemini_session, session_service, session_id)

            # ── TASK A: Frontend → Gemini ──────────────────────────────────────
            # Audio:  send_realtime_input(audio=Blob(...))  — 16kHz PCM from AudioWorklet
            # Text:   send_realtime_input(text=...)         — text typed or TTS transcript
            #
            # DO NOT use send_client_content for text during active conversation.
            # send_client_content is reserved for history seeding (done above).
            async def receive_from_frontend():
                _chunks = 0
                try:
                    while True:
                        data = await websocket.receive()

                        if data.get("type") == "websocket.disconnect":
                            logger.info(f"Frontend disconnected (disconnect message): session={session_id}")
                            break

                        if "bytes" in data and data["bytes"]:
                            _chunks += 1
                            if _chunks == 1:
                                logger.info(f"First audio chunk: {len(data['bytes'])}B (session={session_id})")
                            # Audio PCM 16kHz → Gemini (STT handled natively)
                            await gemini_session.send_realtime_input(
                                audio=types.Blob(
                                    mime_type="audio/pcm;rate=16000",
                                    data=data["bytes"],
                                )
                            )

                        elif "text" in data and data["text"]:
                            try:
                                ctrl = json.loads(data["text"])
                                if ctrl.get("type") == "text_input" and ctrl.get("text"):
                                    # send_realtime_input for in-conversation text.
                                    # The SDK signals end-of-turn for text automatically.
                                    await gemini_session.send_realtime_input(
                                        text=ctrl["text"]
                                    )
                            except (json.JSONDecodeError, KeyError):
                                pass

                except WebSocketDisconnect:
                    logger.info(f"Frontend disconnected: session={session_id}")
                except Exception as e:
                    logger.error(f"Frontend receive error (session={session_id}): {e}")

            # ── TASK B: Gemini → Frontend ──────────────────────────────────────
            # A single ServerContent event can contain MULTIPLE parts at once:
            # e.g. audio chunk + transcript text in the same event.
            # Iterate ALL parts — never assume a single-part event.
            async def receive_from_gemini():
                _audio_sent  = 0
                _resp_count  = 0
                try:
                    async for response in gemini_session.receive():
                        _resp_count += 1

                        if response.server_content:
                            sc = response.server_content

                            # Barge-in: user spoke over the AI — tell browser to clear queued audio
                            if getattr(sc, "interrupted", False):
                                try:
                                    await websocket.send_text(json.dumps({"type": "flush_audio"}))
                                    logger.info(f"Barge-in: flush_audio → browser (session={session_id})")
                                except Exception:
                                    pass

                            # input_audio_transcription: what the CUSTOMER said, in their language.
                            # Arrives on sc.input_transcription (separate from model_turn).
                            if getattr(sc, "input_transcription", None):
                                user_text = getattr(sc.input_transcription, "text", "") or ""
                                if user_text:
                                    logger.info(f"User said [{user_text[:120]}] (session={session_id})")
                                    try:
                                        await websocket.send_text(json.dumps({
                                            "type": "user_transcript",
                                            "text": user_text,
                                        }))
                                    except Exception:
                                        pass

                            # output_audio_transcription: what the ASSISTANT said, in the detected language.
                            # Arrives on sc.output_transcription (separate from model_turn audio parts).
                            if getattr(sc, "output_transcription", None):
                                assistant_text = getattr(sc.output_transcription, "text", "") or ""
                                if assistant_text:
                                    logger.info(f"Assistant said [{assistant_text[:120]}] (session={session_id})")
                                    try:
                                        await websocket.send_text(json.dumps({
                                            "type": "transcript",
                                            "text": assistant_text,
                                        }))
                                    except Exception:
                                        pass

                            if sc.model_turn:
                                for part in sc.model_turn.parts:
                                    # Inline text parts (older SDK path — also forward as transcript)
                                    if part.text:
                                        try:
                                            await websocket.send_text(json.dumps({
                                                "type": "transcript",
                                                "text": part.text,
                                            }))
                                        except Exception:
                                            pass

                                    # Audio PCM 24kHz — stream bytes directly to browser
                                    if part.inline_data and part.inline_data.data:
                                        _audio_sent += 1
                                        if _audio_sent == 1:
                                            logger.info(
                                                f"First Gemini audio: {len(part.inline_data.data)}B"
                                                f" → browser (session={session_id})"
                                            )
                                        await websocket.send_bytes(part.inline_data.data)

                            # Signal the widget that this assistant turn is done so it
                            # can finalise the streaming bubble into one complete message.
                            if getattr(sc, "turn_complete", False):
                                try:
                                    await websocket.send_text(json.dumps({"type": "turn_complete"}))
                                except Exception:
                                    pass

                        # Tool calls — synchronous only (async not supported in 3.1 Flash Live)
                        if response.tool_call and woo_client:
                            function_responses = []
                            for fc in response.tool_call.function_calls:
                                tool_name = fc.name
                                # fc.args is already a dict in google-genai SDK
                                tool_args = dict(fc.args) if fc.args else {}
                                # fc.id may be None in v1alpha — use tool_name as fallback
                                # so Gemini can still correlate the response to the call.
                                call_id = fc.id or tool_name
                                logger.info(
                                    f"Tool call received: {tool_name} id={call_id} "
                                    f"args={tool_args} (session={session_id})"
                                )
                                try:
                                    from agent.tools import execute_tool
                                    tool_exec = await execute_tool(
                                        tool_name=tool_name,
                                        tool_args=tool_args,
                                        session_id=session_id,
                                        woocommerce_service=woo_client,
                                    )
                                    # Log result summary so failures are visible in logs
                                    result_ok = tool_exec.result.get("success", False)
                                    if not result_ok:
                                        logger.warning(
                                            f"Tool {tool_name} returned failure: "
                                            f"{tool_exec.result.get('error', 'no error field')} "
                                            f"(session={session_id})"
                                        )
                                    else:
                                        # Log a brief summary (e.g. product count)
                                        n = len(tool_exec.result.get("products", []))
                                        if n:
                                            logger.info(f"Tool {tool_name} → {n} products (session={session_id})")
                                        else:
                                            logger.info(f"Tool {tool_name} → ok (session={session_id})")

                                    if tool_exec.action and tool_exec.action.get("type") != "noop":
                                        try:
                                            await websocket.send_text(json.dumps({
                                                "type":   "ui_action",
                                                "action": tool_exec.action,
                                            }))
                                        except Exception:
                                            pass
                                    if tool_name in ("add_to_cart", "remove_from_cart", "update_cart_quantity"):
                                        cart_data = tool_exec.result.get("cart", {})
                                        if cart_data and session_service:
                                            try:
                                                await session_service.save_cart(session_id, cart_data)
                                            except Exception:
                                                pass
                                    function_responses.append(
                                        types.FunctionResponse(
                                            name=tool_name,
                                            id=call_id,
                                            response=tool_exec.result,
                                        )
                                    )
                                except Exception as tool_err:
                                    logger.error(
                                        f"Tool execution error {tool_name}: {tool_err} "
                                        f"(session={session_id})",
                                        exc_info=True,
                                    )
                                    function_responses.append(
                                        types.FunctionResponse(
                                            name=tool_name,
                                            id=call_id,
                                            response={"success": False, "error": str(tool_err)},
                                        )
                                    )
                            if function_responses:
                                await gemini_session.send(
                                    input=types.LiveClientToolResponse(
                                        function_responses=function_responses
                                    )
                                )

                    if _resp_count == 0:
                        logger.warning(
                            f"Gemini returned 0 responses — likely wrong api_version or quota issue"
                            f" (session={session_id})"
                        )
                    else:
                        logger.info(f"Gemini session closed after {_resp_count} responses (session={session_id})")

                except Exception as e:
                    logger.error(
                        f"Gemini receive error (session={session_id}): {type(e).__name__}: {e}",
                        exc_info=True,
                    )

            # ── Full-Duplex ────────────────────────────────────────────────────
            frontend_task = asyncio.create_task(receive_from_frontend())
            gemini_task   = asyncio.create_task(receive_from_gemini())

            done, pending = await asyncio.wait(
                [frontend_task, gemini_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

    except Exception as e:
        logger.error(f"A2A session error (session={session_id}): {type(e).__name__}: {e}", exc_info=True)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# OLD ARCHITECTURE — kept for rollback, not deleted
# ───────────────────────────────────────────────────────────────────────────────
# To roll back to HTTP STT→LLM→TTS pipeline:
#   1. Re-enable in main.py:  app.include_router(chat.router)
#                             app.include_router(transcribe.router)
#   2. Set in widget:         const A2A_ENABLED = false;
# ═══════════════════════════════════════════════════════════════════════════════
