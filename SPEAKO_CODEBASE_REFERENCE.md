# Speako — Complete Codebase Reference (LLM Context Document)

> A single, self-contained technical reference for the **Speako** project (agent name **Aria**).
> Written to be handed to an LLM as full context. Covers architecture, every major module,
> data flow, the AI pipeline, integrations, multi-tenancy, deployment, and operations.
> Paths are relative to the repo root `Agentic-ecom-main/`.

---

## 1. What Speako Is

**Speako** is a **multi-tenant SaaS AI shopping assistant**. Merchants install it on their
**Shopify** or **WooCommerce** (or custom-API) store. Their customers talk to **"Aria"** — an
embedded chat widget supporting **voice and text**. Aria searches products, answers questions,
adds to cart, applies coupons, tracks orders, and guides checkout.

- **Product name:** Speako · **Agent name:** Aria · **Owner:** Mohammed Nifli
- **Deployment:** Render (web + worker + beat). Domain example: `https://agentic-ecommerce-mail.onrender.com`
- **One backend:** all code lives in `backend/src/app/`. (`wooagent-backend/` is a dead legacy prototype — ignore it.)

### Tech stack
- **Backend:** Python 3.12, FastAPI, SQLAlchemy (async) + asyncpg, Alembic, Pydantic / pydantic-settings
- **Data:** PostgreSQL (with **pgvector** + **tsvector**), Redis (cache + sessions + Celery broker)
- **Background:** Celery worker + Celery beat
- **AI:** xAI Grok, OpenAI GPT-4o / GPT-4o-mini, Google Gemini (text + Gemini Live voice), OpenAI embeddings, Google Cloud TTS (+ ElevenLabs fallback)
- **Frontend:** a single vanilla-JS widget (`wooagent-widget.js`) using Shadow DOM; a WordPress plugin wraps it for WooCommerce
- **Infra:** Docker (multi-stage), Render Blueprint (`render.yaml`)

---

## 2. Repository Structure

```
/
├── backend/
│   ├── src/app/
│   │   ├── server.py                 FastAPI app factory + lifespan (sets app.state.*)
│   │   ├── config.py                 Settings (pydantic-settings, reads env)
│   │   ├── core/                     database.py, cache.py, security.py, crypto.py, logging, exceptions
│   │   ├── agent/                    THE AI CORE
│   │   │   ├── orchestrator.py       AgentOrchestrator.run → delegates to ask_brain
│   │   │   ├── brain/
│   │   │   │   ├── core.py           ask_brain — the full request pipeline
│   │   │   │   ├── llm_loop.py       run_llm_agent — multi-round tool-calling loop
│   │   │   │   ├── tool_dispatch.py  execute_tool_call (LLM-facing tool execution)
│   │   │   │   ├── fast_intent.py    deterministic handlers (store info, cart view, etc.)
│   │   │   │   └── canned.py         multilingual canned responses
│   │   │   ├── classifier.py         intent classification (Grok LLM + regex fallback)
│   │   │   ├── llm_router.py         4-way LLM routing + circuit breakers
│   │   │   ├── llm_clients.py        provider client singletons
│   │   │   ├── retrieval/            normalizer.py, search.py (hybrid BM25+vector), reranker
│   │   │   ├── guardrails.py         check_input / check_output (anti-hallucination)
│   │   │   ├── prompts/system.py     Aria's persona + rules; filtering.py (lang detect/cleanup)
│   │   │   ├── tools/base.py         tool registry + execute_tool
│   │   │   ├── memory/               session.py (Redis state), facts.py (preferences)
│   │   │   └── voice/synthesis.py    TTSServiceV2 (Google TTS + cache + fallback)
│   │   ├── integrations/
│   │   │   ├── factory.py            create_store_client_for_tenant + per-tenant client cache
│   │   │   ├── shopify/client.py     ShopifyClient (Storefront GraphQL + Admin API) ~1200 lines
│   │   │   ├── woocommerce/          WooCommerceClient + CachedWooCommerceClient
│   │   │   └── custom_api/client.py  CustomApiClient (convention-based REST)
│   │   ├── modules/
│   │   │   ├── tenants/              models.py (Tenant), repository.py, dependencies.py
│   │   │   ├── auth/oauth/shopify.py Shopify OAuth install/callback + widget loader + script tags
│   │   │   ├── billing/              plans, subscriptions, usage metering, quota enforcement
│   │   │   └── analytics/            conversation metrics
│   │   ├── api/v1/
│   │   │   ├── router.py             mounts module routers under /api/v1
│   │   │   ├── public.py             POST /greet, POST /chat, GET /cart (widget, no auth)
│   │   │   ├── voice.py              WS /wooagent/stream (Gemini Live voice)
│   │   │   ├── onboarding.py         POST /onboard self-serve signup
│   │   │   └── health.py             GET /health, GET /ops
│   │   └── workers/
│   │       ├── celery_app.py         Celery config (Redis broker)
│   │       ├── schedules.py          beat schedule
│   │       └── tasks/sync_products.py  catalog sync → product_cache (+ embeddings)
│   ├── static/                       wooagent-widget.js, wooagent-widget.css, onboard.html
│   └── migrations/versions/          Alembic migrations 0001..0012
├── plugins/wordpress/wooagent/       WordPress plugin (mirrors widget, adds REST cart endpoints)
├── infra/docker/                     Dockerfile, Dockerfile.dev, docker-compose.dev.yml
└── render.yaml                       Render Blueprint: speako-web, speako-worker, speako-beat
```

---

## 3. High-Level Architecture & Data Flow

```
Customer's browser (widget)
   │   text → HTTP POST /api/v1/chat?shop=<domain>
   │   voice → WS /wooagent/stream (Gemini Live)
   ▼
FastAPI (speako-web)
   │   resolve tenant from ?shop= → per-tenant store client (factory, DB credentials)
   │   AgentOrchestrator.run → ask_brain (the pipeline)
   │        ├─ retrieval: product_cache (BM25+vector) → live store API fallback
   │        ├─ LLM tool-loop (Grok/GPT/Gemini) → tools hit the store client
   │        ├─ guardrails (anti-hallucination)
   │        └─ TTS synthesis
   ▼
PostgreSQL (tenants, product_cache, billing, analytics)   Redis (sessions, caches, Celery)
   ▲
Celery worker + beat (speako-worker / speako-beat)
        sync_products → fetch catalog via store API → embed → upsert product_cache
        webhooks, retries, analytics rollups, monthly invoicing
```

**Two request paths:**
- **Text:** widget → `POST /api/v1/chat` → `AgentOrchestrator.run` → `ask_brain` (deterministic Brain, no voice dependency). Returns text + `audio_base64` (TTS) + `ui_actions`.
- **Voice:** widget → `WS /wooagent/stream` → Gemini Live pipeline (STT + reasoning + TTS in one hop) via a `PipelineRouter`.

---

## 4. The AI Agent Pipeline (most important)

### 4.1 Entry point — `agent/orchestrator.py`
`AgentOrchestrator.run(session_id, user_message, store_context, page_context, language, cart_context)`
is a **thin wrapper** that injects dependencies (`store_client`, `session_service`, `redis`,
`db_session_factory`) and calls `ask_brain(...)`. Output dict:
`{session_id, text, response_text, speech_text, language, ui_actions, actions, suggested_replies}`
(plus `audio_base64`, `audio_format` added at the API layer for `/chat`).

### 4.2 `ask_brain` — `agent/brain/core.py` (the pipeline)
Ordered stages:

1. **Input sanitize + guardrail** — `sanitize_text` then `check_input`. Off-topic → `_blocked_response`; empty → `_empty_response`.
2. **Parallel pre-processing** (`asyncio.gather`): intent classification, session load, session meta load.
3. **Language resolution** — `detect_language`; prefer detected non-English, else last saved language; persist.
4. **Cart fetch** — `_fetch_cart`: caller `cart_context` → live `store_client.get_cart` → Redis cache → empty default.
5. **Intent routing** (six branches):
   - **OFF_TOPIC** (conf ≥ 0.75) → `off_topic_response`.
   - **CHITCHAT** (conf ≥ 0.75) → `chitchat_response`, *unless* it's a bare affirmation following an assistant question (`_is_affirmative_followup`) → fall through to LLM with history.
   - **STORE_INFO / CART_ACTION** (or shipping/returns/payment/cart-view/remove keyword intents) → `run_fast_intent` (deterministic store calls).
   - **Store-not-connected guard** — if `store_client.has_credentials is False` and intent is product-related/browse → `_not_connected_result` ("This store isn't fully connected yet…"). Prevents hallucinated products when a tenant has no usable Shopify token.
   - **Retrieval pre-fetch** for SEARCH / PRODUCT_DETAIL / INVENTORY — `_run_retrieval` → `hybrid_search`. Tracks `retrieval_ran` (call completed) vs `retrieval_found` (≥1 product). A new SEARCH with 0 results clears stale `last_products` to avoid hallucination.
     - **Hard stop** when retrieval ran, found nothing, no prior products, and not a generic browse:
       - `_looks_unintelligible(query)` → `_unintelligible_result` (warm "didn't catch that" for keyboard-mash like "asdfgh").
       - else `_no_products_result` ("couldn't find any products matching that…").
   - **Fast specific handlers** (keyword-matched): order tracking, compare, availability, add-to-cart, buy-intent.
6. **Primary LLM agent** — if still unresolved and an LLM is available: fetch `store_catalog` (categories + sale signal only — **never** product names/prices, to avoid hallucination), then `run_llm_agent` (multi-round tool loop).
7. **Fallbacks** — `run_fast_intent` → `handle_product_discovery` → `_help_fallback_result`.
8. **Post-processing** — `extract_next_suggestions`, `strip_function_markup`, `cap_to_sentences` (≤4).
9. **Output guardrail** — `build_retrieved_context` then `check_output` (grounding). On `OutputValidationError` → `retry_with_stricter_prompt`.
10. **Schema validation** — `AgentResponse.model_validate`.
11. **Persistence + telemetry** — trim history (~28 turns), `session_service.update_session`, `SessionFacts.update`, beta-logger.

**Deterministic result helpers** (multilingual: en/hi/ml/ta/te/bn/kn): `_no_products_result`,
`_not_connected_result`, `_unintelligible_result`, `_help_fallback_result`, `_blocked_response`,
`_empty_response`. **Guards:** `_is_generic_browse`, `_looks_unintelligible`,
`has_credentials` check.

### 4.3 Intent classification — `agent/classifier.py`
- **9 intents:** SEARCH, CART_ACTION, CHITCHAT, ORDER_STATUS, CHECKOUT, STORE_INFO, PRODUCT_DETAIL, INVENTORY, OFF_TOPIC.
- **`IntentResult`**: `intent, confidence, query, product_ref, quantity, via, latency_ms` (+ `is_shopping`, `needs_llm`, `is_fast_path`).
- **Tier 1 — LLM:** xAI **Grok** (`grok-3-mini-fast`), OpenAI-compatible at `https://api.x.ai/v1`, temp 0, JSON output, ~8s timeout.
- **Tier 2 — regex fallback** (`_RegexClassifier`): prioritized patterns, default SEARCH (conf 0.6), ~0 ms. Used if the LLM is unavailable/times out.

### 4.4 LLM routing — `agent/llm_router.py` + `llm_clients.py`
Four providers, each with a **circuit breaker** (3 failures → 30s cooldown) and a **global per-turn deadline (~16s)**:
1. **xAI Grok** — primary brain reasoning (temp 0.2, ~512 tokens, parallel tool calls), ~9s cap.
2. **GPT-4o-mini** — fallback 1, ~8s.
3. **Gemini (2.x Flash)** — fallback 2 (uses google-genai SDK; OpenAI tool schema converted to Gemini `FunctionDeclaration`).
4. **GPT-4o** — escalation (address FSM / complex multi-tool), tried first for escalations.
All responses normalized to `{text, tool_calls:[{id,name,arguments}], llm_route}`.

### 4.5 Retrieval — `agent/retrieval/`
**L0 normalizer** (`normalizer.py`, pure ~0.5ms) → `NormalizedQuery {raw, clean, lang, min_price,
max_price, in_stock_only, has_attribute, tokens, cache_key}`. Steps: language detect (Unicode
script), NFC, lowercase, price extraction (`under/below/over/between`), in-stock hint, strip noise
phrases + leading affirmations, punctuation cleanup, synonym expansion (tshirt→t-shirt, mobile→phone),
tokenize, cache key. `has_attribute` (color/size/capacity) → **skip L2** semantic cache (avoids
red-vs-blue fuzzy mismatch).

**`hybrid_search`** (`search.py`) tiers:
- **L1** exact-key Redis cache (<3 ms).
- **L2** semantic Redis cache by query embedding + filter signature (~15 ms), skipped when `has_attribute`.
- **L3** Postgres `product_cache`: **BM25** (`search_vector @@ plainto_tsquery`, `ts_rank`) + **vector** (`embedding <-> query_embedding`, pgvector), fused via RRF + boost → top results. Write-through to L1/L2.
- **Live fallback** — if cache empty/unavailable: `store_client.search_products(...)`. (This is what runs when the Celery sync worker hasn't populated `product_cache`.)

### 4.6 Guardrails — `agent/guardrails.py`
- **`check_input`** (pre-LLM, regex): blocks off-topic (news, weather, recipes, coding, medical, etc.), strips PII. Raises `InputBlocked`.
- **`check_output`** (post-LLM, anti-hallucination), validates the reply against `build_retrieved_context` (retrieved IDs / prices / name tokens / attributes):
  1. **Product ID grounding** — mentioned IDs must be in retrieved set.
  2. **Name grounding** — `_PRODUCT_MENTION_RE` finds Capitalized product-like phrases; skips negation context ("No Casio G-Shock"); requires a model-number token; a distinctive token must appear in retrieved names.
  3. **Price grounding** — mentioned prices must match retrieved prices.
  4. **Attribute grounding** — invented size/color (needs ≥2) → fail.
  5. **PII stripping** (`_redact_pii`).
  6. **`strip_inline_prices`** — *structural rule:* spoken text must contain **no numbers** (price/stock render on the product card, not in prose). Strips currency/stock numbers and cleans leftover fragments (e.g. orphan `.0`, "with left", doubled words). Failure → `OutputValidationError` → stricter-prompt retry.
- **Language matching** — long replies must use the expected script for the detected language.

### 4.7 System prompt / persona — `agent/prompts/system.py`
"You are Aria…". **Cardinal rules:** (1) ZERO hallucination — every product name/spec/stock must
come from a tool call; the catalog lists **categories only**, never product names. (2) PRICE/STOCK
ABSOLUTE — never write a price/stock number in text (the UI shows them). (3) VOICE style — ≤3
sentences, no markdown/bullets/numbers, no "Certainly!/Absolutely!", end with a question or next
step; optional `NEXT:` quick-reply chips.

### 4.8 Tools — `agent/tools/base.py` + `agent/brain/tool_dispatch.py`
~15 tools mapped to store-client methods, each returning a `(result, ui_action)` pair:
`search_products` → `show_products`; `get_product_details` → `show_product_detail`;
`check_inventory` → `show_availability`; `add_to_cart` → `add_to_cart` (client-side action — the
widget performs the real add); plus `remove_from_cart`, `update_cart_quantity`, `get_cart`,
`find_variants`, `get_reviews`, `compare_products`, `apply_coupon`, `get_best_coupon`, `get_orders`,
`get_categories`, `get_store_info`. `run_llm_agent` (`brain/llm_loop.py`) runs up to **3 rounds**;
after round 2 it forces text-only.

### 4.9 Memory — `agent/memory/`
- **`session.py`** (`SessionService`, Redis + in-memory fallback, ~2h TTL): conversation history
  (trimmed ~16–28 turns), cart snapshot, customer email, last products, language meta. Per-session lock.
- **`facts.py`** (`SessionFactsService`): preferred size/color, max budget, last product, detected
  category. **Topic switching** drops product-specific prefs on category change; budget persists.
  `format_for_prompt` injects prefs into the system prompt.

### 4.10 TTS — `agent/voice/synthesis.py`
`TTSServiceV2`: Google Cloud TTS primary (per-language voice map), ElevenLabs/Groq fallback.
Two-tier cache: Redis L1 (base64, 24h) + object storage L2 (raw audio, 30d), memory-LRU when Redis
down. `synthesize(text, language)` strips `<think>`, applies `make_speech_friendly` (no emoji/markdown),
caches by text+lang, returns base64. `audio_format()` reports the format.

---

## 5. Integrations (store clients)

### 5.1 Factory — `integrations/factory.py`
- **`create_store_client_for_tenant(tenant, redis_client)`** (HTTP/WS path): per-tenant client
  **cache** keyed by `tenant_id`, TTL 300s, lock-guarded (avoids httpx pool churn).
- **`create_store_client(platform, credentials)`** / **`_sync`**: no-cache variants for Celery/DI.
- **`_build_client(tenant)`** dispatches on `tenant.platform`:
  - shopify → `ShopifyClient(store_domain, storefront_token, admin_token)`
  - woocommerce → `CachedWooCommerceClient(WooCommerceClient(...))`
  - custom_api → `CustomApiClient(base_url, api_key)`
- `invalidate_tenant_client(tenant_id)` evicts + closes.

### 5.2 Shopify — `integrations/shopify/client.py`
Dual API: **Storefront GraphQL** (products, cart; `X-Shopify-Storefront-Access-Token`) and
**Admin REST/GraphQL** (orders, discounts, shop info; `X-Shopify-Access-Token`).
- **`has_credentials`** = `domain AND (storefront_token OR admin_token)` — drives the not-connected guard.
- **`search_products(...)`**: cache → **Storefront** GraphQL search → **Admin fallback** (`_admin_search_products`) if Storefront returns nothing (missing/revoked token or products not published to the Online Store channel).
- **`_admin_search_products`** — typo-tolerant search:
  - **Two-step fetch:** Shopify query `status:active AND (tok1 OR tok2 …)` (stopword-filtered OR of significant tokens so big catalogs return relevant items, not the first 60 alphabetical); if a *search* matched nothing, fetch a browse page (`status:active`, ~150) so the local ranker has a catalog.
  - **Client-side fuzzy scoring** via `_token_word_match(token, word)`: 2 = substring (both ≥3 chars), 1 = subsequence/`difflib` ratio ≥0.75; plus **adjacent-word joins** so "G-Shock" → "gshock" lets "gshk"/"gshook"/"gshcok" match. Returns only score > 0.
- **Normalization:** `_normalize_product_node` (Storefront) and `_normalize_admin_gql_node` (Admin) produce the same shape: `{id, name, price, sale_price, regular_price, stock_status, stock_quantity, image_url, permalink, short_description, attributes, variations_summary, on_sale}`. Admin lacks `compareAtPriceRange`/`availableForSale` → derive sale from variant `compareAtPrice`, stock from `inventoryQuantity` (None = untracked = purchasable).
- **Cart:** native Storefront `cartLinesAdd/Update`, cart id persisted in Redis per session. (Note: the widget actually performs cart writes via Shopify's native AJAX endpoints — see §9.) Coupons via `cartDiscountCodesUpdate`; `get_best_coupon` scans Admin price rules. Orders via Admin REST.

### 5.3 WooCommerce / Custom API
- **WooCommerce** (`woocommerce/client.py`): three-path product search — custom plugin endpoint → public Store API (`/wc/store/v1/products`) → authenticated `wc/v3`. `CachedWooCommerceClient` adds Redis caching. `verify=False` for LocalWP self-signed certs.
- **Custom API** (`custom_api/client.py`): convention-based REST (`/products/search`, `/cart/add`, `/coupons/best`, `/orders`, `/store/info`, …), optional `Authorization: Bearer`, `follow_redirects=False` (SSRF hardening).

---

## 6. Multi-Tenancy & Tenant Resolution

- **`Tenant`** (`modules/tenants/models.py`): `id` (UUID), `name`, `email` (unique), `plan`,
  `is_active`, `hashed_password` (Argon2), `platform`. **Shopify:** `shopify_domain` (unique, plain —
  it's the lookup key), `shopify_access_token` + `shopify_storefront_token` (**EncryptedText**),
  `shopify_scope`, `shopify_installed_at`. **WooCommerce:** `woocommerce_store_url` + encrypted
  `consumer_key`/`consumer_secret`. **Custom:** `custom_api_base_url`, `custom_api_key` (plain — also
  an inbound lookup key).
- **Resolution order** (`modules/tenants/dependencies.py`):
  | Endpoint | Dependency | P1 | P2 | P3 |
  |---|---|---|---|---|
  | `/greet`, `/chat`, `/cart` | `get_tenant_store_client` | `?shop=` | `X-Tenant-ID` | `app.state.store_client` |
  | `WS /wooagent/stream` | `resolve_tenant_store_client_for_ws` | `?shop=` | `?tenant_id=` | global |
  | admin routes | `get_authenticated_tenant` | Bearer JWT `sub` | — | — |
  The widget always sends `?shop=<domain>`, so requests resolve to that tenant's stored credentials.
- **Encryption** (`core/crypto.py`): `EncryptedText` = Fernet (AES-128-CBC+HMAC), prefix `enc:v1:`.
  Safe-degrade: no key → plaintext passthrough; legacy plaintext read as-is; bad key → warn + plaintext.

---

## 7. Shopify OAuth (zero-touch credential provisioning)

`modules/auth/oauth/shopify.py`. A merchant installs once and **manages no tokens** — both the Admin
token and Storefront token are provisioned automatically.

- **Scopes** include Admin (`read_products, write_script_tags, read_script_tags, read_orders,
  read_customers`) **and** `unauthenticated_*` (Storefront) scopes so a Storefront token can be minted.
- **`GET /shopify/install?shop=`**: validates `.myshopify.com`, stores a `state` nonce in Redis (600s),
  redirects to Shopify consent. Requests an **offline** token (no `grant_options[]=per-user`) so it
  does **not** expire after 24h.
- **`GET /shopify/callback`**: verify HMAC → verify `state` → exchange `code` for Admin token →
  **`_verify_admin_token`** (GET `shop.json`; on failure show an honest **error page**, not "Installed!")
  → **`_create_storefront_token`** (`storefrontAccessTokenCreate` mutation) → **save tenant** (on DB
  error, surface an error page — do not claim success) → **`_register_script_tag`** (widget loader;
  non-fatal). Logs `admin=yes storefront=yes/no`.
- **`GET /shopify/widget-loader.js?shop=`**: dynamic loader registered as a Shopify Script Tag; sets
  `window.wooagent_config` (backend_url, store_name, currency, primary_color, position, platform:shopify,
  shop) and inlines the widget JS. `no-cache`, `Access-Control-Allow-Origin: *`.
- **`POST /shopify/setup`**: manual script-tag registration for testing.

> **Answer to "do merchants need a Storefront token?":** No. OAuth captures the Admin token and mints
> the Storefront token automatically; the Admin API fallback keeps products loading even if the
> Storefront token is missing/revoked. The only way a store has no token is if it never completed the
> real OAuth install (e.g. a stale/legacy record) — re-installing fixes it.

---

## 8. Data Layer & Background Workers

### 8.1 Postgres (Alembic `migrations/versions/0001..0012`)
Key tables: `tenants`, `product_cache`, `conversations`, `messages`, `cart_items`, `orders`,
`usage_records`, `subscriptions`, `plans`, `webhook_events`, `conversation_metrics`, `refresh_tokens`,
`users`.

**`product_cache`** is the search index (L3). Built incrementally across migrations:
- `(tenant_id, platform_id)` UPSERT key; columns: name, description, price, currency, image_url,
  in_stock, stock_quantity, category_slug, tags, permalink, cached_at.
- **`embedding vector(1536)`** (pgvector; OpenAI `text-embedding-3-small`) — semantic search, ivfflat then **HNSW** index.
- **`search_vector tsvector`** (GIN, trigger-maintained) — BM25 full-text.
- B-tree on `tenant_id`. Webhook idempotency via unique `webhook_event_id`.

### 8.2 Core
- `core/database.py`: async engine (pool 20 + overflow 40, pre-ping, recycle 1800s), `AsyncSessionLocal`,
  `get_db` dependency, `worker_session()` (NullPool per Celery task to avoid event-loop reuse bugs).
- `core/cache.py`: global async Redis (`init_cache`, `get_redis`).
- `core/security.py`: **Argon2** password hashing, **PyJWT** (HS256) tokens, HMAC widget request
  signing (300s replay window), `sanitize_text`, `mask_email`. (Not jose/passlib.)

### 8.3 Celery — `workers/`
- `celery_app.py`: Redis broker/backend, JSON serialization, `acks_late`, `reject_on_worker_lost`,
  `prefetch_multiplier=1`, time limits 900s/840s.
- `schedules.py` (beat): `process-pending-webhooks` (60s), `aggregate-daily-analytics` (00:05 UTC),
  `invoice-subscriptions` (monthly), `sync-products-nightly` (02:30 UTC), `sync-products-diff`
  (every 4h), `retry-failed-actions` (60s).
- `tasks/sync_products.py`: per-tenant — build client from **DB credentials** → fetch full catalog
  (Shopify Bulk Ops → Storefront cursor → single-page fallback; Woo paginate; custom paginate) →
  normalize (adapters) → batch-embed (OpenAI, 50/batch, degrades to None) → **UPSERT** product_cache
  (ON CONFLICT) → cleanup stale (>48h) → invalidate L1/L2 retrieval cache. Diff sync triggers full
  sync if catalog drift > 10%. Redis lock prevents concurrent syncs.

> **Operational note:** if the worker + beat aren't running, `product_cache` stays empty and search
> falls back to the live store API every request (slower, and limited to the ~150-item Admin fallback
> window). Running worker+beat is the scale-proof path (semantic search over the whole catalog).

---

## 9. The Widget — `backend/static/wooagent-widget.js`

Single vanilla-JS file (~4800 lines), **Shadow DOM** isolated, dark theme default (light opt-in).
Mirrored to `plugins/wordpress/wooagent/widget/wooagent-widget.js` for WooCommerce.

- **Config:** `window.wooagent_config` (set by the Shopify widget-loader or the WP plugin):
  `agent_api_url, store_name, currency, primary_color, platform, shop, …`.
- **Session:** `S` object; `sessionId` persisted in localStorage (`_wa_sid_v2`); last ~20 messages cached.
- **UI:** FAB (floating button), header (mute/theme/clear/close), messages log, voice orb + live
  transcript pill, text input bar, suggested-reply chips, toasts.
- **Message flow:** text → `POST {agent_api_url}/api/v1/chat?shop=…` (sends session_id, message,
  language, cart_context, page_context); voice → `WS /wooagent/stream`. Response →
  `processAction()` per `ui_action` → `speakWithFallback(audio_base64,…)`.
- **`processAction` types:** `show_products` (render cards), `show_product_detail`, `add_to_cart`
  (calls `addToCartDispatch`), `remove_from_cart`, `apply_coupon`, `cart_updated` (badge).
- **Native cart (Shopify):** `addToCartShopify` → `POST /cart/add.js`; `fetchCartShopify` → `/cart.js`;
  remove → `/cart/change.js`. Variant resolution from `/products/<handle>.js`. The agent's `add_to_cart`
  is a **client-side action** — the widget performs the real add against the store's native cart.
  Checkout via `/checkout` with `checkout[...]` prefilled address params.
- **Native cart (WooCommerce):** custom REST `POST /wp-json/wooagent/v1/cart/add|remove`, `GET …/cart`.
- **Robust JSON:** `_parseJsonSafe` parses by body (not the `content-type` header) because Shopify's
  `.js` endpoints can return JSON with a `text/javascript` content-type.
- **TTS playback:** `playAudioB64` decodes base64 → Blob → `Audio`, with start (8s) and max (90s)
  watchdogs, mic paused while speaking. `speakWithFallback` plays `audio_base64` (browser-TTS fallback removed — audio only plays when the backend returns `audio_base64`).
- **Address FSM** (Woo checkout): idle → phone → email → complete; draft persisted across navigation.

---

## 10. Public API Reference

- **`POST /api/v1/greet`** — opening greeting; resolves tenant, builds greeting (new vs returning,
  general vs product page), synthesizes TTS, returns `{greeting_text, audio_base64, audio_format,
  language_detected, has_cart, is_returning, suggested_replies}`. Records 1 credit.
- **`POST /api/v1/chat`** — main text turn. Resolves tenant store client (Depends), enforces quota,
  runs `AgentOrchestrator.run`, **synthesizes TTS for every reply** (mirrors greet), returns
  `{text, response_text, speech_text, language, ui_actions, actions, suggested_replies, audio_base64,
  audio_format}`. (Older `chat.py` 410 stub removed; this is the live endpoint.)
- **`GET /api/v1/cart?session_id=`** — normalized cart for any platform. No auth.
- **`WS /wooagent/stream`** — voice. `GET /wooagent/ws-token` issues an HMAC token first. Validates
  session length, token, connection rate limit; resolves tenant; enforces voice concurrency cap and
  quota (voice = 3 credits, text = 1); delegates to `PipelineRouter`. Binary PCM in/out + JSON control
  messages.
- **`GET /api/v1/health`** (Redis ping) and **`GET /api/v1/ops`** (retry queue, dead-letter, Celery
  depth, webhook backlog).
- **`POST /api/v1/onboard`** — self-serve signup (WooCommerce/custom; Shopify uses OAuth). Validates
  platform credentials, creates tenant, enrolls plan, queues initial sync, returns a widget snippet.
  `POST /onboard/test-connection` validates creds; `GET /onboard/lookup` maps custom_api_key → tenant.

---

## 11. Billing & Analytics
- **`plans`** (free/starter/pro/enterprise; `features` JSON e.g. `allow_voice`), **`subscriptions`**
  (status, period), **`usage_records`** (credit ledger).
- **Quota** (`modules/billing/dependencies.py`): text turn = 1 credit, voice = 3. Free tier = 50
  all-time (no reset). Paid: atomic Redis credit reservation seeded from DB usage; HTTP 402 over limit;
  voice gated by `plan.features.allow_voice`. Fails open if Redis down.
- **Analytics** (`conversation_metrics`): daily rollup — conversations, completed purchases, revenue,
  avg session seconds.

---

## 12. Configuration (env vars) — `config.py`
Important keys (see `config.py` for all): `PLATFORM` (shopify|woocommerce|custom_api), `DATABASE_URL`
(**must** be `postgresql+asyncpg://…`), `REDIS_URL`, `CELERY_BROKER_URL` (optional), `JWT_SECRET_KEY`,
`SHARED_SECRET`, `ENCRYPTION_KEY` (Fernet). Shopify: `SHOPIFY_API_KEY`/`SHOPIFY_API_SECRET` (OAuth),
`SHOPIFY_API_VERSION`, and (dev/single-store only) `SHOPIFY_STORE_DOMAIN`/`SHOPIFY_STOREFRONT_TOKEN`/
`SHOPIFY_ADMIN_TOKEN`. WooCommerce: `WOOCOMMERCE_STORE_URL`/`CONSUMER_KEY`/`CONSUMER_SECRET`. AI:
`GROK_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `GOOGLE_TTS_API_KEY`,
`ELEVENLABS_API_KEY`. Misc: `BACKEND_URL`, `STORE_NAME`, `STORE_CURRENCY`, object-storage settings.
Production guard blocks startup on weak `JWT_SECRET_KEY`/`SHARED_SECRET`.

> Production multi-tenant credentials live in the DB per tenant; env `SHOPIFY_*` only feed the global
> dev/single-store client.

---

## 13. Deployment

- **`infra/docker/Dockerfile`** — multi-stage (build wheels → slim runtime). `CMD ["sh","-c","alembic
  upgrade head && uvicorn src.app.server:app --host 0.0.0.0 --port ${PORT:-8000}"]`. Uses `sh -c` so
  `&&` and `${PORT}` are shell-interpreted (Render's command box can't run shell).
- **`render.yaml`** — three services from the same image / env group `speako`:
  - `speako-web` (health `GET /api/v1/health`, `preDeployCommand: alembic upgrade head`)
  - `speako-worker` (`celery -A src.app.workers.celery_app worker`)
  - `speako-beat` (`celery -A src.app.workers.celery_app beat`)
- After deploy, set Shopify Partner app URLs: App URL `…/api/v1/shopify/install`, Redirect
  `…/api/v1/shopify/callback`.
- **server.py** lifespan sets `app.state`: `redis`, `store_client` (global, from `PLATFORM` env),
  `session_service`, `tts_service`, `storage_client`, `audio_logger`, `orchestrator`. CORS `*`
  (widget loads cross-origin); `/static/*` gets CORS headers; security headers (HSTS in prod). Fires an
  initial `sync_products.delay()`.

---

## 14. WordPress Plugin — `plugins/wordpress/wooagent/`
`wooagent.php` (v1.4.x): enqueues the widget JS/CSS on the storefront, sets `window.wooagent_config`,
and exposes REST cart endpoints (`POST /wp-json/wooagent/v1/cart/add|remove`, `GET …/cart`) backed by
the WC session. Creates a `wp_wooagent_sessions` table. After editing the widget JS, mirror it here and
bump `WOOAGENT_VERSION` for cache-busting.

---

## 15. Known Issues / Operational Notes
- Widget JS is cached by Shopify/browsers — re-register the script tag or hard-refresh after JS changes.
- ngrok free tier drops WebSocket after ~30s — voice is unreliable on ngrok; fine on Render.
- `DATABASE_URL` must use the `postgresql+asyncpg://` scheme (plain `postgresql://` pulls psycopg2,
  which isn't installed).
- Run **worker + beat** on Render or `product_cache` stays empty (search falls back to live API).
- Per-user (online) Shopify tokens expire in ~24h — the install uses **offline** tokens to avoid this.
- A tenant can exist with empty tokens (legacy/abandoned install) → `has_credentials` is False →
  the not-connected guard fires; re-install via OAuth to provision tokens.

---

## 16. Recent Fixes (changelog context)
- **Typo-tolerant search** — Admin fallback two-step fetch + fuzzy/subsequence/adjacent-join scoring;
  gibberish input gets a friendly "didn't catch that" reply (`commit 6ae62c1`).
- **OAuth hardening** — offline token, `shop.json` verification, honest error page (no false
  "Installed!"), non-swallowed DB-save errors, `has_credentials` + store-not-connected guard
  (`commit e8f5e8c`).
- **Widget/UX** — clean price/stock stripping (no `.0` / "with left" / "is is" fragments), removed the
  false "Unexpected non-JSON" cart error (parse Shopify cart response by body not content-type), and
  TTS on **every** reply (`/chat` now synthesizes audio like `/greet`) (`commit a9552ee`).

---

*End of reference. For exact behavior, read the cited files under `backend/src/app/`.*
```
