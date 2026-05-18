# Speako — System Architecture

## Overview

Speako is a multi-tenant SaaS platform. One backend serves unlimited merchant stores. Each merchant installs the app on their Shopify or WooCommerce store — an AI chat widget ("Aria") appears automatically, letting their customers shop via voice or text.

---

## High-level architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        CLIENT SIDE                              │
│                                                                 │
│   Shopify Store              WooCommerce (WordPress)            │
│   ┌──────────────┐           ┌──────────────────────┐          │
│   │ Script Tag   │           │  WordPress Plugin    │          │
│   │ (auto-inject)│           │  (wooagent.php)      │          │
│   └──────┬───────┘           └──────────┬───────────┘          │
│          │                              │                       │
│          └──────────┬───────────────────┘                       │
│                     │                                           │
│            wooagent-widget.js  (Shadow DOM chat widget)         │
└─────────────────────┼───────────────────────────────────────────┘
                      │  HTTPS / WSS
┌─────────────────────▼───────────────────────────────────────────┐
│                    SPEAKO BACKEND (FastAPI)                      │
│                   backend/src/app/                              │
│                                                                 │
│  ┌──────────┐  ┌──────────┐  ┌────────────┐  ┌─────────────┐  │
│  │  API v1  │  │  Agent   │  │Integrations│  │   Modules   │  │
│  │ (HTTP +  │  │  (AI     │  │(Shopify /  │  │ (Tenants /  │  │
│  │  WSS)    │  │  Core)   │  │ WooComm.)  │  │  Billing)   │  │
│  └──────────┘  └──────────┘  └────────────┘  └─────────────┘  │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │                    core/                                  │  │
│  │  database · cache · security · logging · exceptions      │  │
│  └──────────────────────────────────────────────────────────┘  │
└───────────────────────┬─────────────────┬───────────────────────┘
                        │                 │
              ┌─────────▼──┐        ┌─────▼──────┐
              │ PostgreSQL │        │   Redis    │
              │ (tenants,  │        │ (sessions, │
              │  billing,  │        │  cache,    │
              │  analytics)│        │  queues)   │
              └────────────┘        └────────────┘
```

---

## Backend layer breakdown

### 1. `server.py` — App factory

- Creates FastAPI app
- Lifespan: boots store client (Shopify or WooCommerce based on `PLATFORM`), Redis, session service, TTS
- Mounts API router at `/api/v1`, voice WebSocket at `/wooagent/stream`, static files at `/static/`
- CORS middleware (allows `*` for widget cross-origin loading)

### 2. `config.py` — Settings

- Single `Settings` class via pydantic-settings
- Reads all env vars from `.env`
- Cached singleton via `@lru_cache`
- Key groups: app, database, redis, jwt, platform, shopify, woocommerce, LLM keys, voice keys, store display

### 3. `core/` — Cross-cutting infrastructure

| File | Purpose |
|------|---------|
| `database.py` | SQLAlchemy async engine, `Base`, `AsyncSessionLocal`, `init_db()` |
| `cache.py` | Redis connection pool, `init_cache()` |
| `security.py` | JWT (PyJWT), password hashing (argon2), HMAC-SHA256 widget verification |
| `exceptions.py` | `NotFoundError`, `ConflictError`, `UnauthorizedError` |
| `logging.py` | structlog JSON logging setup |
| `middleware.py` | Request ID injection, error handling |
| `pagination.py` | Generic paginated response helper |

### 4. `agent/` — AI core

The brain of Aria. Platform-agnostic — works the same for Shopify and WooCommerce.

```
agent/
├── orchestrator.py      Main agent loop: receives user message → calls LLM → executes tools → returns response
├── llm_router.py        Routes to GPT-4o / GPT-4o-mini / Groq LLaMA / Gemini based on context
├── llm_clients.py       Initialises OpenAI, Groq, Gemini clients
├── gemini_client.py     Gemini Live WebSocket client + WS token generation
├── extractor.py         Structured data extraction from LLM output
├── guardrails.py        Input/output safety checks
├── beta_logger.py       PostgreSQL session telemetry (optional)
├── tools/base.py        Tool definitions (search_products, add_to_cart, etc.) + execute_tool()
├── prompts/
│   ├── system.py        Aria's system prompt — personality, rules, language handling
│   ├── filtering.py     Language detection, speech text normalization
│   └── voice.py         Gemini Live voice-specific prompt
├── memory/
│   ├── session.py       Redis session state (conversation history, cart snapshot, language)
│   └── facts.py         Persistent facts store per session
└── voice/
    ├── synthesis.py     TTS — Google Cloud primary (TTSServiceV2), fallbacks
    ├── tts_fallback.py  ElevenLabs / Azure / Groq TTS fallbacks
    └── transcription.py STT — Groq Whisper primary, Deepgram fallback
```

**LLM routing logic:**
```
Short query + few tools  → GPT-4o-mini (fast, cheap)
Complex reasoning        → GPT-4o (accurate)
Dravidian languages      → Gemini (better multilingual)
High-throughput / speed  → Groq LLaMA (fastest)
Voice stream             → Gemini Live (STT + reasoning + TTS in one WebSocket)
```

### 5. `integrations/` — Store platform adapters

Platform-agnostic via `BaseStoreClient` ABC. The agent calls the same methods regardless of platform.

```
integrations/
├── base/
│   ├── commerce.py    BaseStoreClient ABC — defines all methods agents can call
│   ├── product.py     Product data types
│   ├── cart.py        Cart data types
│   └── order.py       Order data types
├── shopify/
│   └── client.py      ShopifyClient — Storefront GraphQL + Admin REST (~1200 lines)
│                      Products, cart (Redis session), orders, discounts, store info
├── woocommerce/
│   ├── client.py      WooCommerceClient — WC REST API v3
│   └── cache.py       CachedWooCommerceClient — Redis caching layer
├── custom_api/
│   └── client.py      Generic REST adapter (for headless / custom platforms)
└── factory.py         create_store_client(platform, credentials) → BaseStoreClient
```

**Key methods on BaseStoreClient:**
- `search_products(query, filters)` → product list
- `get_product_details(id)` → full product
- `add_to_cart(session_id, variant_id, quantity)` → updated cart
- `remove_from_cart(session_id, item_key)` → updated cart
- `get_cart(session_id)` → cart state
- `apply_coupon(session_id, code)` → discount result
- `get_orders(session_id)` → order history
- `get_store_info()` → store metadata

### 6. `modules/` — Multi-tenant domain

Each module follows the same structure: `models.py → repository.py → service.py → router.py → schemas.py → dependencies.py`

| Module | DB Table(s) | Purpose |
|--------|-------------|---------|
| `tenants` | `tenants` | Merchant registration, Shopify OAuth fields (`shopify_domain`, `shopify_access_token`) |
| `auth` | `refresh_tokens` | JWT login/logout, Shopify OAuth install/callback flow |
| `users` | `users` | User accounts (belong to tenants) |
| `billing` | `plans`, `subscriptions`, `usage_records` | Subscription plans, usage metering |
| `conversations` | `conversations`, `messages` | Chat session records |
| `products` | `product_cache` | Product catalogue cache |
| `carts` | `cart_items` | Cart state persistence |
| `orders` | `orders` | Order history |
| `webhooks` | `webhook_events` | Outbound webhook delivery to merchants |
| `analytics` | `conversation_metrics` | Usage analytics per tenant |

### 7. `api/v1/` — HTTP + WebSocket endpoints

```
api/v1/
├── router.py     Mounts all module routers under /api/v1
├── health.py     GET  /api/v1/health          → {"status":"ok","redis":true}
├── public.py     POST /api/v1/greet           → greeting + suggested replies
│                 GET  /api/v1/cart            → cart state (no auth)
├── chat.py       POST /api/v1/chat            → 410 Gone (use WebSocket)
└── voice.py      WS   /wooagent/stream        → Gemini Live A2A relay
```

**Module routers (all under /api/v1):**
- `/auth/login`, `/auth/logout`
- `/shopify/install`, `/shopify/callback`, `/shopify/setup`, `/shopify/widget-loader.js`
- `/tenants/`, `/users/`, `/billing/plans`, `/billing/subscription`
- `/products/search`, `/conversations/chat`, `/carts/`, `/orders/`
- `/webhooks/woocommerce/{tenant_id}`, `/webhooks/shopify/{tenant_id}`
- `/analytics/summary`, `/analytics/metrics`

### 8. `workers/` — Background tasks

Celery workers for async processing:
- `tasks/billing.py` — usage aggregation, subscription renewals
- `tasks/analytics.py` — metrics rollup
- `tasks/webhooks.py` — outbound webhook retry queue

---

## Database schema

```
tenants ──────────────────────────────────────────────────────────
  id, name, email, plan, is_active
  shopify_domain, shopify_access_token, shopify_scope, shopify_installed_at
  created_at, updated_at

users ────────────────────────────────────────────────────────────
  id, tenant_id (FK), email, hashed_password, role, is_active

refresh_tokens ───────────────────────────────────────────────────
  id, user_id (FK), token_hash, expires_at, revoked

plans ────────────────────────────────────────────────────────────
  id, name, price, currency, features (JSON), limits (JSON)

subscriptions ────────────────────────────────────────────────────
  id, tenant_id (FK), plan_id (FK), status, current_period_end

usage_records ────────────────────────────────────────────────────
  id, tenant_id (FK), metric, value, recorded_at

conversations ────────────────────────────────────────────────────
  id, tenant_id (FK), session_id, platform, started_at

messages ─────────────────────────────────────────────────────────
  id, conversation_id (FK), role, content, created_at

cart_items ───────────────────────────────────────────────────────
  id, tenant_id (FK), session_id, product_id, variant_id, quantity

orders ───────────────────────────────────────────────────────────
  id, tenant_id (FK), session_id, platform_order_id, status, total

product_cache ────────────────────────────────────────────────────
  id, tenant_id (FK), product_id, data (JSON), cached_at

webhook_events ───────────────────────────────────────────────────
  id, tenant_id (FK), event_type, payload (JSON), status, attempts

conversation_metrics ─────────────────────────────────────────────
  id, tenant_id (FK), date, total_conversations, messages_sent
```

---

## Request flows

### Text chat flow
```
Customer types message
  → POST /api/v1/conversations/chat  {session_id, message}
  → auth middleware (optional for widget)
  → agent/orchestrator.py
      → session_service.get_history(session_id)     [Redis]
      → llm_router.route(message, history)          [picks LLM]
      → LLM generates tool calls
      → execute_tool(tool_name, args, store_client) [calls Shopify/WooCommerce]
      → LLM generates final response
      → session_service.save_history(session_id)    [Redis]
  ← response text + actions (add_to_cart, show_products, etc.)
  → widget renders response + executes actions
```

### Voice flow (Gemini Live)
```
Customer speaks
  → browser MediaRecorder captures audio chunks
  → WebSocket /wooagent/stream?session_id=...&token=...
  → voice.py relays audio to Gemini Live WebSocket
  → Gemini Live: STT + reasoning + TTS in one round trip
  → audio response streamed back to browser
  → browser plays audio
  → widget also receives tool call actions (add_to_cart, etc.)
```

### Shopify merchant install flow
```
Merchant clicks Install
  → GET /api/v1/shopify/install?shop=store.myshopify.com
  → generate state nonce (stored in Redis, 10min TTL)
  → redirect to https://store.myshopify.com/admin/oauth/authorize?...
  → merchant approves permissions
  → GET /api/v1/shopify/callback?code=xxx&shop=xxx&hmac=xxx&state=xxx
  → verify HMAC signature + state nonce
  → POST Shopify token endpoint → get access_token
  → upsert Tenant row with shopify_domain + access_token
  → register widget script tag on store (POST /admin/api/script_tags.json)
  → return success HTML page
  → widget auto-appears on all store pages
```

### Widget load flow (Shopify)
```
Customer visits Shopify store
  → Shopify injects: <script src="backend/api/v1/shopify/widget-loader.js">
  → widget-loader.js returns inlined JS:
      window.wooagent_config = { agent_api_url, store_name, platform, ... }
      + full wooagent-widget.js code (inlined to avoid CORS issues)
  → widget renders Shadow DOM chat button in bottom-right
  → customer clicks → widget opens → POST /api/v1/greet → greeting shown
```

---

## Technology choices

| Concern | Choice | Reason |
|---------|--------|--------|
| Web framework | FastAPI | async, fast, great OpenAPI docs |
| ORM | SQLAlchemy 2.0 async | native async, type-safe, Alembic migrations |
| DB driver | asyncpg | fastest PostgreSQL async driver |
| Cache | Redis (aioredis) | session state, product cache, rate limiting |
| Auth | PyJWT + argon2-cffi | both in pyproject.toml, no jose/passlib |
| LLM | OpenAI + Groq + Gemini | cost/speed routing |
| Voice STT | Groq Whisper | fastest, cheapest |
| Voice TTS | Google Cloud TTS | best quality multilingual |
| Voice stream | Gemini Live WebSocket | STT+reasoning+TTS in one hop |
| Widget | Vanilla JS + Shadow DOM | no framework deps, works on any site |
| Deploy | Docker + Railway | simple, free tier available |

---

## Multi-tenancy model

**Current:** Single global store client from `.env` (dev mode)

**Production path:**
1. Merchant installs → Tenant row created with `shopify_domain` + `shopify_access_token`
2. Per-request middleware reads `X-Tenant-ID` header or `shop` param
3. Loads tenant from DB → creates `ShopifyClient(domain, token)` for that merchant
4. Agent uses that client → fully isolated per merchant

**Data isolation:** All domain tables have `tenant_id` foreign key. Queries always filter by tenant_id.

---

## Security

| Mechanism | Implementation |
|-----------|---------------|
| JWT auth | PyJWT HS256, 60min expiry, refresh tokens in DB |
| Password hashing | Argon2 (argon2-cffi) |
| Widget request verification | HMAC-SHA256 over `timestamp.path.body` with `SHARED_SECRET` |
| Shopify OAuth HMAC | Verified on every callback using `SHOPIFY_API_SECRET` |
| CSRF protection | State nonce stored in Redis (10min TTL) per OAuth flow |
| CORS | Allow-all for widget (cross-origin by design), restrictable per tenant |
