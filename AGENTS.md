# Speako — Codex Context

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
/backend/src/app/
├── server.py                    FastAPI app factory + lifespan
├── config.py                    Settings (pydantic-settings, reads .env)
├── core/
│   ├── database.py              SQLAlchemy async engine + session factory
│   ├── cache.py                 Redis caching helpers
│   ├── security.py              sanitize_text, JWT helpers
│   └── exceptions.py            custom exception classes
│
├── agent/                       ─── AI AGENT CORE ───
│   ├── orchestrator.py          thin coordinator → delegates to brain/core.py
│   ├── brain/                   ─── CONSOLIDATED BRAIN PIPELINE ───
│   │   ├── core.py              MAIN ENTRY: 9-step pipeline (input → classify → retrieve → LLM → guardrail → output)
│   │   ├── llm_loop.py          LLM agent loop (max 3 rounds, tool execution, retry on hallucination)
│   │   ├── fast_intent.py       deterministic handlers (~50% of intents bypass LLM: orders, compare, add-to-cart)
│   │   ├── text_utils.py        output formatting: cap_to_sentences, strip_function_markup, extract_inline_calls
│   │   ├── tool_dispatch.py     tool argument coercion + dispatch
│   │   ├── address.py           address collection state machine
│   │   └── canned.py            deterministic chitchat / off-topic responses
│   ├── guardrails.py            ─── HALLUCINATION KILLER ───
│   │   ├── check_input()        off-topic blocklist + PII redaction (pre-LLM)
│   │   └── check_output()       6 checks: IDs, names, prices, attributes, language, stock (post-LLM)
│   │       ├── validate_spoken_text()  voice transcript monitor (secondary)
│   │       ├── build_retrieved_context()  extract grounding sets from tool results
│   │       └── safe_fallback()         deterministic fallback when retry fails
│   ├── classifier.py            intent classifier (Groq LLaMA + keyword fallback): 9 classes
│   ├── llm_router.py            4-way LLM routing: GPT-4o → GPT-4o-mini → xAI Grok → Gemini (circuit breakers + timeouts)
│   ├── prompts/system.py        Aria's system prompt — personality, cardinal rules, purchase flow
│   ├── tools/base.py            tool definitions (search_products, add_to_cart, check_inventory, etc.)
│   ├── memory/
│   │   ├── session.py           Redis session store (cart, history, meta, language lock)
│   │   └── facts.py             SessionFacts: remembers last product, preferences across turns
│   ├── voice/                   ─── VOICE ARCHITECTURE ───
│   │   ├── coordinator.py       VoiceTurnCoordinator: event loop, brain turns, spoken_truth validation
│   │   ├── synthesis.py         TTS (Google Cloud primary)
│   │   ├── transcription.py     STT (Groq Whisper)
│   │   ├── audio_logger.py      debug audio recording
│   │   ├── pipelines/
│   │   │   ├── pipeline_a.py    Gemini Live: full voice pipeline (STT → tool calls → speak)
│   │   │   ├── pipeline_c.py    text-only fallback pipeline
│   │   │   └── router.py        selects provider (Gemini Live / OpenAI Realtime)
│   │   └── providers/
│   │       ├── base.py          BaseVoiceProvider interface
│   │       ├── gemini_live.py   Gemini Live WebSocket provider
│   │       └── openai_realtime.py  OpenAI Realtime API provider (gpt-realtime-2.1-mini)
│   ├── retrieval/
│   │   ├── hybrid_search.py     vector + keyword hybrid search (pgvector)
│   │   ├── normalizer.py        query normalization
│   │   ├── reranker.py          cross-encoder reranking
│   │   └── cache.py             product cache (Redis)
│   └── modules/offers/          ─── MERCHANT PROMOTIONS ───
│       ├── recommendations.py   fetches active offers for brain prompt
│       └── ... (model, repository, service, router)
│
├── integrations/                store platform adapters
│   ├── shopify/client.py        Shopify Storefront + Admin API (~1200 lines)
│   ├── woocommerce/             WooCommerce REST + Redis cache
│   └── factory.py               create_store_client(platform, credentials)
│
├── modules/                     multi-tenant domain modules
│   ├── tenants/                 tenant CRUD + Shopify OAuth fields + store config
│   ├── auth/                    JWT auth + Shopify OAuth install/callback
│   │   └── oauth/shopify.py     OAuth flow + widget loader + script tags
│   ├── users/                   customer user management
│   ├── products/                product sync + cache management
│   ├── conversations/           conversation history
│   ├── carts/                   cart operations
│   ├── orders/                  order capture (webhooks)
│   ├── webhooks/                Shopify webhook handlers
│   ├── analytics/               usage analytics
│   ├── billing/                 subscription + usage metering
│   ├── admin/                   operator-only: subscription management
│   └── offers/                  merchant promotions / dead stock dashboar
│
├── api/v1/
│   ├── router.py                mounts all module routers
│   ├── health.py                GET /api/v1/health
│   ├── public.py                POST /api/v1/greet, GET /api/v1/cart
│   ├── voice.py                 WS /wooagent/stream (WebSocket endpoint)
│   └── ingest.py                product ingestion endpoint
│
├── static/                      widget JS + CSS (served at /static/)
│   ├── wooagent-widget.js
│   └── wooagent-widget.css
│
└── migrations/versions/         Alembic migrations (0017 +)
```

---

## The 9-Step Brain Pipeline (`agent/brain/core.py`)

This is the central data flow for EVERY customer request:

```
USER INPUT
  │
  ├── [Voice] Pipeline A → Gemini Live/OpenAI Realtime → ask_brain tool call
  └── [Text]  POST /greet → AgentOrchestrator → ask_brain()

  1. INPUT SANITIZATION  → sanitize_text() + check_input()
  2. PARALLEL PRE-PROCESS → intent classifier + session load + meta load
  3. LANGUAGE RESOLUTION   → English-decay (3-turn lock)
  4. CART FETCH            → live API → Redis → empty fallback
  5. INTENT ROUTING
       ├── OFF_TOPIC (conf≥0.75) → canned response
       ├── CHITCHAT (conf≥0.75)  → canned (bypasses if follow-up)
       ├── STORE_INFO / CART_ACTION → fast_intent() (~0ms, no LLM)
       ├── SEARCH → hybrid_search() → results or LLM
       └── ORDER / COMPARE / AVAILABILITY → fast_intent()
  6. LLM AGENT LOOP (max 3 rounds)
       ├── build_system_prompt() → Aria + store context + promotions
       ├── route_and_call() → GPT-4o-mini → Grok → Gemini
       └── Tool execution → product data → more LLM rounds
  7. OUTPUT GUARDRAIL (6 checks)
       ├── check_output() → IDs, names, prices, attributes, stock, language
       ├── FAIL → retry_with_stricter_prompt()
       └── FAIL → safe_fallback()
  8. SCHEMA VALIDATION → AgentResponse model_validate
  9. SPEECH PROCESSING → make_speech_friendly() → session persistence
```

---

## Guardrails (`agent/guardrails.py`)

Six checks run on EVERY LLM output (Step 7 above):

| # | Check | What it prevents | Trigger |
|---|-------|------------------|---------|
| 1 | Product ID grounding | LLM inventing fake IDs | IDs in text not in retrieved set |
| 2 | Price grounding | LLM fabricating prices | prices not matching retrieved data |
| 3 | Attribute validation | LLM inventing colours/sizes | single ungrounded attribute value |
| 4 | PII stripping | Leaked emails, phones, cards | pattern match + replace |
| 4b | Inline price/stock strip | Raw numbers in spoken output | structural regex removal |
| 5 | Language script match | Wrong-language reply | script absent from long response |
| 6 | Stock-status verification | False stock claims | contradicting retrieved stock_map |

On failure → `retry_with_stricter_prompt()` with a product table + strict grounding prompt. If retry also fails → `safe_fallback()` (deterministic localized message). Voice path has a secondary monitor (`validate_spoken_text`) that checks spoken transcript against brain's grounding.

---

## Voice architecture

```
Browser MediaRecorder → WebSocket /wooagent/stream
                           │
                    VoiceTurnCoordinator
                     (agent/voice/coordinator.py)
                     ├── session management
                     ├── brain turn execution (15s timeout)
                     ├── spoken_truth grounding tracking
                     └── transcript validation (buffered, checked at turn_complete)

Provider: Gemini Live (pipeline_a.py) or OpenAI Realtime (openai_realtime.py)
  Both delegate to ask_brain tool → brain/core.py → same pipeline
```

---

## Guardrail patterns to know

- **Product name grounding**: two-path system (model-number tokens require literal match; digit-free names use fuzzy matching at 0.80 threshold against retrieved full names)
- **Promoted products**: only counts per offer type are injected into system prompt (never product names) — LLM must call `search_products(on_sale=True)` for actual names
- **Stock checking**: LLM explicitly forbidden from declaring stock without `check_inventory` tool call. Check 6 cross-references stock declarations against retrieved data
- **Transcript buffering**: voice transcript deltas are streamed raw to widget; validation runs on full buffered text at `turn_complete`. If the voice model hallucinates, a `transcript_correction` event is sent

---

## Edit these files for common tasks

| Task | File |
|------|------|
| Change Aria's personality / rules | `backend/src/app/agent/prompts/system.py` |
| Add a hallucination check | `backend/src/app/agent/guardrails.py` — add to check_output() |
| Add/change a tool | `backend/src/app/agent/tools/base.py` + `tool_dispatch.py` |
| Add a deterministic handler | `backend/src/app/agent/brain/fast_intent.py` |
| Change LLM routing / add provider | `backend/src/app/agent/llm_router.py` |
| Change voice provider | `backend/src/app/agent/voice/pipelines/router.py` |
| Change voice pipeline behavior | `backend/src/app/agent/voice/pipelines/pipeline_a.py` |
| Change OpenAI Realtime config | `backend/src/app/agent/voice/providers/openai_realtime.py` |
| Change widget loader JS | `backend/src/app/modules/auth/oauth/shopify.py` → `widget_loader()` |
| Change widget UI | `backend/static/wooagent-widget.js` (also sync to plugins/wordpress/) |
| Shopify OAuth | `backend/src/app/modules/auth/oauth/shopify.py` |
| Add migration | create `backend/migrations/versions/000N_description.py` |
| Add merchant offers/promotions | `backend/src/app/modules/offers/` |

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
| `OPENAI_API_KEY` | GPT-4o / GPT-4o-mini + OpenAI Realtime |
| `GROQ_API_KEY` | STT (Whisper) + Groq LLaMA |
| `GEMINI_API_KEY` | Gemini Live WebSocket |
| `GOOGLE_TTS_API_KEY` | Google Cloud TTS |
| `ELEVENLABS_API_KEY` | ElevenLabs TTS fallback |
| `OPENAI_REALTIME_MODEL` | `gpt-realtime-2.1-mini` (default) |
| `DATABASE_URL` | PostgreSQL async URL (asyncpg) |
| `REDIS_URL` | Redis URL (default: `redis://redis:6379/0`) |
| `JWT_SECRET_KEY` | JWT signing key |
| `SHARED_SECRET` | HMAC widget request verification |
| `BACKEND_URL` | Public backend URL (ngrok or production) |
| `STORE_NAME` | Display name shown in widget |
| `STORE_CURRENCY` | Currency symbol (e.g. `$`) |

---

## Deployment (Render)

- Blueprint: `render.yaml` (repo root) — defines THREE services: `speako-web`,
  `speako-worker`, `speako-beat`, all from `infra/docker/Dockerfile` (context `backend/`).
- **The worker + beat are required** — without them product sync, webhooks, retries,
  billing and analytics never run (searches fall back to the live store API).
- Migrations run via the web service `preDeployCommand: alembic upgrade head`.

After deploying, update Shopify Partner app URLs:
- App URL: `https://YOUR-BACKEND-URL/api/v1/shopify/install`
- Redirect URL: `https://YOUR-BACKEND-URL/api/v1/shopify/callback`

---

## Known issues / fixes applied

- `restart` does not reload `.env` — use `up -d app` to recreate the container
- `SHOPIFY_API_KEY==value` (double `=`) is a typo that breaks parsing — always use single `=`
- Widget JS cached by Shopify — re-register script tag after every JS change
- ngrok free tier drops WebSocket after ~30s — voice unreliable on ngrok; works fine on a stable host (Render)
- Migration env.py needs `sys.path.insert` to find `src` module — already applied
- Security uses PyJWT + argon2-cffi (NOT jose/passlib — those aren't installed)
- **No guardrail test suite** — hallucination scenarios untested; rely on manual validation
- **OpenAI Realtime transcript is delta-validated not full-utterance** — per-delta check was replaced with buffered check at turn_complete; transcript_correction event sent on hallucination
