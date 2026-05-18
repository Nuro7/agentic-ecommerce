# Speako — Claude Context

## What this project is
Multi-tenant SaaS AI shopping assistant. Merchants install the app on their Shopify or WooCommerce store. Their customers talk to "Aria" via an embedded chat widget — voice or text. Aria searches products, adds to cart, applies coupons, and guides checkout.

**Product name:** Speako  
**Agent name:** Aria  
**Owner:** Mohammed Nifli  

---

## ONE backend — never touch wooagent-backend/

All work goes in **`backend/src/app/`**.  
`wooagent-backend/` is the old single-store prototype — ignore it.

---

## How to run locally

```bash
# From repo root — start Postgres + Redis + FastAPI (hot reload)
docker compose -f infra/docker/docker-compose.dev.yml up -d

# View live logs
docker compose -f infra/docker/docker-compose.dev.yml logs -f app

# Run migrations (first time or after new migration files)
docker compose -f infra/docker/docker-compose.dev.yml exec app alembic upgrade head

# Recreate app container after .env change (restart doesn't reload .env)
docker compose -f infra/docker/docker-compose.dev.yml up -d app
```

API docs: http://localhost:8000/docs  
Health check: http://localhost:8000/api/v1/health

---

## Project structure

```
/
├── backend/                        ← ALL backend work goes here
│   ├── src/app/
│   │   ├── server.py               FastAPI app factory + lifespan
│   │   ├── config.py               Settings (pydantic-settings, reads .env)
│   │   ├── core/                   database, cache, logging, exceptions, security
│   │   ├── agent/                  AI agent core
│   │   │   ├── orchestrator.py     main agent loop — LLM + tool calls
│   │   │   ├── llm_router.py       4-way LLM routing (GPT-4o / mini / Groq / Gemini)
│   │   │   ├── tools/base.py       tool definitions + execute_tool()
│   │   │   ├── prompts/system.py   Aria's personality + system prompt
│   │   │   ├── memory/session.py   Redis session state
│   │   │   └── voice/synthesis.py  TTS (Google Cloud primary)
│   │   ├── integrations/           store platform adapters
│   │   │   ├── shopify/client.py   Shopify Storefront + Admin API (~1200 lines)
│   │   │   ├── woocommerce/        WooCommerce REST + Redis cache
│   │   │   └── factory.py          create_store_client(platform, credentials)
│   │   ├── modules/                multi-tenant domain modules
│   │   │   ├── tenants/            tenant CRUD + Shopify OAuth fields
│   │   │   ├── auth/               JWT auth + Shopify OAuth install/callback
│   │   │   │   └── oauth/shopify.py  OAuth flow + widget loader + script tags
│   │   │   ├── billing/            subscription + usage metering
│   │   │   └── analytics/          usage analytics
│   │   └── api/v1/
│   │       ├── router.py           mounts all module routers
│   │       ├── health.py           GET /api/v1/health
│   │       ├── public.py           POST /api/v1/greet, GET /api/v1/cart
│   │       └── voice.py            WS /wooagent/stream (Gemini Live)
│   ├── static/                     widget JS + CSS (served at /static/)
│   │   ├── wooagent-widget.js      the chat widget (shared Shopify + WooCommerce)
│   │   └── wooagent-widget.css
│   ├── migrations/versions/        Alembic migrations
│   ├── railway.toml                Railway deployment config
│   └── pyproject.toml              Python deps
│
├── plugins/wordpress/wooagent/     WordPress plugin for WooCommerce stores
│   └── widget/                     JS/CSS source (sync to backend/static/ after edits)
│
└── infra/docker/                   Dockerfiles + Compose files
    ├── Dockerfile                  Production image (used by Railway)
    ├── Dockerfile.dev              Dev image (hot reload)
    └── docker-compose.dev.yml      Local dev stack
```

---

## Key app.state values (set in server.py lifespan)

| Key | Type | Description |
|-----|------|-------------|
| `app.state.store_client` | `ShopifyClient` or `CachedWooCommerceClient` | platform-aware store client |
| `app.state.session_service` | `SessionService` | Redis session store |
| `app.state.tts_service` | `TTSServiceV2` | TTS synthesis |
| `app.state.redis` | `aioredis` | raw Redis client |

Platform is set by `PLATFORM=shopify` or `PLATFORM=woocommerce` in `.env`.

---

## Shopify OAuth flow

```
Merchant clicks Install
  → GET /api/v1/shopify/install?shop=store.myshopify.com
  → redirects to Shopify OAuth consent
  → GET /api/v1/shopify/callback?code=xxx&shop=xxx&hmac=xxx
  → exchanges code for access token
  → saves tenant to DB (tenants table, shopify_domain + shopify_access_token)
  → registers widget script tag on store
  → shows success page
```

For testing without OAuth: `POST /api/v1/shopify/setup` with `{"backend_url": "https://..."}`.

---

## Voice / agent flow

```
User speaks → browser MediaRecorder
           → WebSocket /wooagent/stream  (Gemini Live — STT + reasoning + TTS in one hop)

Text fallback:
           → POST /api/v1/greet    on widget open
           → POST /api/v1/chat     (returns 410 — widget uses WebSocket)
```

---

## Edit these files for common tasks

| Task | File |
|------|------|
| Change Aria's personality / rules | `backend/src/app/agent/prompts/system.py` |
| Add/change a tool | `backend/src/app/agent/tools/base.py` + `orchestrator.py` |
| Change LLM routing | `backend/src/app/agent/llm_router.py` |
| Change widget loader JS | `backend/src/app/modules/auth/oauth/shopify.py` → `widget_loader()` |
| Change widget UI | `backend/static/wooagent-widget.js` (also sync to plugins/wordpress/) |
| Shopify OAuth | `backend/src/app/modules/auth/oauth/shopify.py` |
| Add migration | create `backend/migrations/versions/000N_description.py` |

---

## Widget — after editing JS/CSS

1. Edit `backend/static/wooagent-widget.js`
2. Copy to `plugins/wordpress/wooagent/widget/wooagent-widget.js`
3. Bump `WOOAGENT_VERSION` in `plugins/wordpress/wooagent/wooagent.php`
4. Hard-refresh browser: `Ctrl+Shift+R`

For Shopify, the widget-loader endpoint inlines the JS — re-register script tag after changes:
```bash
curl -X POST http://localhost:8000/api/v1/shopify/setup \
  -H "Content-Type: application/json" \
  -d '{"backend_url": "https://YOUR-NGROK-URL.ngrok-free.app"}'
```

---

## Environment variables

| Key | Purpose |
|-----|---------|
| `PLATFORM` | `shopify` or `woocommerce` |
| `SHOPIFY_STORE_DOMAIN` | e.g. `mystore.myshopify.com` |
| `SHOPIFY_STOREFRONT_TOKEN` | Shopify Storefront API token |
| `SHOPIFY_ADMIN_TOKEN` | Shopify Admin API token (custom app) |
| `SHOPIFY_API_KEY` | Partner app Client ID (OAuth) |
| `SHOPIFY_API_SECRET` | Partner app Client Secret (OAuth) |
| `SHOPIFY_API_VERSION` | e.g. `2025-01` |
| `WOOCOMMERCE_STORE_URL` | WordPress site URL |
| `WOOCOMMERCE_CONSUMER_KEY` | WC REST API key |
| `WOOCOMMERCE_CONSUMER_SECRET` | WC REST API secret |
| `OPENAI_API_KEY` | GPT-4o / GPT-4o-mini |
| `GROQ_API_KEY` | STT (Whisper) + Groq LLaMA |
| `GEMINI_API_KEY` | Gemini Live WebSocket |
| `GOOGLE_TTS_API_KEY` | Google Cloud TTS |
| `ELEVENLABS_API_KEY` | ElevenLabs TTS fallback |
| `DATABASE_URL` | PostgreSQL async URL (asyncpg) |
| `REDIS_URL` | Redis URL (default: `redis://redis:6379/0`) |
| `JWT_SECRET_KEY` | JWT signing key |
| `SHARED_SECRET` | HMAC widget request verification |
| `BACKEND_URL` | Public backend URL (ngrok or production) |
| `STORE_NAME` | Display name shown in widget |
| `STORE_CURRENCY` | Currency symbol (e.g. `$`) |

---

## Railway deployment

- Root directory: `backend/`
- Config file: `backend/railway.toml`
- Dockerfile: `infra/docker/Dockerfile`
- Uses `$PORT` env var automatically

After deploying, update Shopify Partner app URLs:
- App URL: `https://YOUR-RAILWAY-URL/api/v1/shopify/install`
- Redirect URL: `https://YOUR-RAILWAY-URL/api/v1/shopify/callback`

---

## Known issues / fixes applied

- `restart` does not reload `.env` — use `up -d app` to recreate the container
- `SHOPIFY_API_KEY==value` (double `=`) is a typo that breaks parsing — always use single `=`
- Widget JS cached by Shopify — re-register script tag after every JS change
- ngrok free tier drops WebSocket after ~30s — voice unreliable on ngrok; works fine on Railway
- Migration env.py needs `sys.path.insert` to find `src` module — already applied
- Security uses PyJWT + argon2-cffi (NOT jose/passlib — those aren't installed)
- Static files need CORS headers for Shopify cross-origin loading — handled by middleware in server.py
