# Agentic Commerce — Claude Context

## What this project is
AI-powered voice shopping assistant for WooCommerce/Shopify/custom stores.
Customers talk to "Aria" via a chat widget embedded in WordPress or Shopify.
Aria searches products, adds to cart, applies coupons, and guides checkout — voice or text.

---

## ONE backend: `backend/`

All work goes in **`backend/src/app/`**. This is the single multi-tenant backend built with clean architecture.
`wooagent-backend/` is the old single-store prototype — no longer needed, can be deleted.

### How to run (dev, hot-reload)
```bash
# From repo root
docker compose -f infra/docker/docker-compose.dev.yml up -d

# View logs
docker compose -f infra/docker/docker-compose.dev.yml logs -f app

# Restart after code change
docker compose -f infra/docker/docker-compose.dev.yml restart app

# Run migrations
docker compose -f infra/docker/docker-compose.dev.yml exec app alembic upgrade head

# Or run directly
cd backend && uvicorn src.app.server:app --reload --port 8000
```

API docs: http://localhost:8000/docs
Ngrok dashboard: http://localhost:4040

---

## Project structure

```
/
├── backend/                     ← ONE backend (multi-tenant SaaS)
│   └── src/app/
│       ├── server.py            FastAPI app factory + lifespan
│       ├── config.py            Settings (pydantic-settings, reads .env)
│       ├── core/                database, cache, logging, exceptions
│       ├── agent/               AI agent core
│       │   ├── orchestrator.py  main agent loop — LLM + tool calls
│       │   ├── gemini_client.py Gemini Live client + WS token helpers
│       │   ├── llm_router.py    4-way LLM routing (GPT-4o / mini / Groq / Gemini)
│       │   ├── llm_clients.py   LLM client initialisation
│       │   ├── extractor.py     structured data extraction
│       │   ├── beta_logger.py   PostgreSQL session telemetry
│       │   ├── tools/base.py    tool definitions + execute_tool()
│       │   ├── prompts/
│       │   │   ├── system.py    orchestrator system prompt builder
│       │   │   ├── filtering.py language detection + speech text helpers
│       │   │   └── voice.py     Gemini Live voice prompt
│       │   ├── memory/
│       │   │   ├── session.py   Redis session state service
│       │   │   └── facts.py     session facts store
│       │   └── voice/
│       │       ├── synthesis.py TTS — Google Cloud primary (TTSServiceV2)
│       │       ├── tts_fallback.py  ElevenLabs / Azure / Groq / browser TTS
│       │       └── transcription.py STT — Groq Whisper + Deepgram fallback
│       ├── integrations/        store platform adapters
│       │   ├── base/commerce.py BaseStoreClient ABC
│       │   ├── woocommerce/     WooCommerce REST API client + Redis cache
│       │   ├── shopify/         Shopify Storefront + Admin API
│       │   ├── custom_api/      generic REST store adapter
│       │   └── factory.py       create_store_client(platform, credentials)
│       ├── modules/             multi-tenant domain modules
│       │   ├── tenants/         tenant registration + management
│       │   ├── auth/            JWT auth + Shopify OAuth
│       │   ├── users/           user accounts
│       │   ├── billing/         subscription + usage metering
│       │   ├── conversations/   chat session records
│       │   ├── products/        product catalogue
│       │   ├── carts/           cart state
│       │   ├── orders/          order history
│       │   ├── webhooks/        outbound webhook delivery
│       │   └── analytics/       usage analytics
│       └── api/v1/
│           ├── router.py        mounts all module routers
│           ├── health.py        GET /api/v1/health
│           ├── public.py        POST /api/v1/greet  (widget open)
│           ├── chat.py          POST /api/v1/chat   (returns 410 — use WebSocket)
│           └── voice.py         WS /wooagent/stream (Gemini Live A2A relay)
│
├── plugins/                     ← client-side plugins / SDKs
│   ├── wordpress/
│   │   └── wooagent/            WordPress plugin (folder name fixed for WP)
│   │       ├── wooagent.php     plugin entry — registers scripts, REST routes
│   │       ├── widget/
│   │       │   ├── wooagent-widget.js   chat widget UI + voice logic
│   │       │   └── wooagent-widget.css  widget styles
│   │       ├── includes/        PHP helper classes (API, auth, settings)
│   │       └── admin/           WordPress admin UI
│   ├── shopify/                 Shopify app (future)
│   └── js-sdk/                  headless JS embed SDK (future)
│
├── infra/                       ← infrastructure / deployment
│   ├── docker/                  Dockerfiles + compose files
│   ├── k8s/                     Kubernetes manifests
│   └── terraform/               cloud provisioning
│
├── pyproject.toml               Python deps + tool config
├── alembic.ini                  DB migrations config
└── .env.example                 environment variable template
```

---

## Key app.state values (set in backend/src/app/server.py lifespan)

| Key | Type | Description |
|---|---|---|
| `app.state.store_client` | `CachedWooCommerceClient` | default store (dev/single-tenant mode) |
| `app.state.session_service` | `RedisSessionService` | Redis session store |
| `app.state.tts_service` | `TTSServiceV2` | TTS synthesis |
| `app.state.redis` | `aioredis` | raw Redis client |

In production the tenant middleware resolves a per-request store client via `app.state.store_client_factory`.

---

## Voice / agent flow

```
User speaks → browser MediaRecorder
           → WebSocket /wooagent/stream (Gemini Live — STT + reasoning + TTS in one hop)

OR (text fallback):
           → POST /api/v1/greet      on widget open
           → POST /api/v1/chat       HTTP agent (returns 410 — use WebSocket)
```

---

## Agent behaviour — edit these files

| Task | File |
|---|---|
| Change Aria's personality / rules | `backend/src/app/agent/prompts/system.py` |
| Add/change a tool | `backend/src/app/agent/tools/base.py` + `orchestrator.py` |
| Change LLM routing | `backend/src/app/agent/llm_router.py` |
| Change voice / TTS | `backend/src/app/agent/voice/synthesis.py` |
| Change Gemini Live config | `backend/src/app/api/v1/voice.py` |

---

## Widget — after editing JS/CSS
Always bump `WOOAGENT_VERSION` in `plugins/wordpress/wooagent/wooagent.php`:
```php
define('WOOAGENT_VERSION', '1.4.33'); // increment this
```
Then hard-refresh the browser (`Ctrl+Shift+R`).

---

## Environment variables

| Key | Purpose |
|---|---|
| `WOOCOMMERCE_STORE_URL` | WordPress store URL |
| `WOOCOMMERCE_CONSUMER_KEY` | WC REST API key |
| `WOOCOMMERCE_CONSUMER_SECRET` | WC REST API secret |
| `OPENAI_API_KEY` | GPT-4o / GPT-4o-mini |
| `GROQ_API_KEY` | STT (Whisper) + Groq LLaMA |
| `GEMINI_API_KEY` | Gemini Live WebSocket |
| `GOOGLE_TTS_API_KEY` | Google Cloud TTS |
| `ELEVENLABS_API_KEY` | ElevenLabs TTS fallback |
| `DATABASE_URL` | PostgreSQL async URL |
| `REDIS_URL` | Redis URL (default: `redis://redis:6379/0`) |
| `JWT_SECRET_KEY` | JWT signing key |
| `SHARED_SECRET` | HMAC widget request verification |
| `NGROK_AUTHTOKEN` | Ngrok tunnel (for local WordPress) |
| `STORE_NAME` | Display name shown in widget |
| `STORE_CURRENCY` | Currency symbol (e.g. `₹`) |

---

## Known issues / fixes applied

- `sa.Enum(create_type=False)` does not work in SQLAlchemy 2.0 — use `postgresql.ENUM(create_type=False)` instead (fixed in migration 0002)
- `CachedWooCommerceClient` param is `wc_client=`, not `wc=` (fixed in main.py)
- Widget JS cached by WordPress — always bump `WOOAGENT_VERSION` after JS edits
