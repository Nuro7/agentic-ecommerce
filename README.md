<<<<<<< HEAD
# agentic-ecommerce
=======
# WooAgent - Agentic Shopping Assistant for WooCommerce

WooAgent is a voice-first, real-time shopping assistant for WooCommerce powered by Gemini Live A2A (audio-to-audio) WebSocket streaming.

- **WordPress plugin** (`/wooagent`) embeds a floating chat/voice widget and exposes secured store REST endpoints.
- **FastAPI backend** (`/wooagent-backend`) relays audio between the browser and Gemini Live, executes WooCommerce tool calls, and manages sessions.
- **Redis** stores sessions (2-hour TTL) and WooCommerce response cache.

## Architecture Overview

```
Browser (Widget JS)
  │  PCM 16kHz audio / text_input (WebSocket binary/JSON)
  ▼
FastAPI /wooagent/stream (WebSocket relay)
  │  Gemini Live A2A (google-genai SDK)
  ├─► Gemini 3.1 Flash Live (STT + reasoning + TTS built-in)
  │     └─ Tool calls → WooCommerceClient (3-tier fallback + Redis cache)
  │
  └─► Browser: PCM 24kHz audio + ui_action JSON
```

**Primary interaction path**: single persistent WebSocket — Gemini handles speech recognition, reasoning, and speech synthesis natively. No separate STT/TTS microservices are needed.

**Legacy HTTP `/chat` endpoint** is disabled (returns `410 Gone`). The agent/orchestrator code and Groq/OpenAI LLM router remain in the codebase for rollback purposes but are not active.

---

## Project Structure

```
Agentic-ecom-main/
├── wooagent/                         # WordPress plugin (PHP, v1.4.31)
│   ├── wooagent.php                  # Plugin entry point, DB table, widget enqueue
│   ├── admin/admin-page.php          # Admin settings UI
│   ├── includes/
│   │   ├── class-wooagent-auth.php   # HMAC + WP nonce validation
│   │   ├── class-wooagent-api.php    # REST endpoint routing
│   │   └── class-wooagent-settings.php
│   └── widget/
│       ├── wooagent-widget.js        # Browser WebSocket client, AudioWorklet, UI
│       └── wooagent-widget.css
├── wooagent-backend/                 # FastAPI service (Python 3.11+)
│   ├── main.py                       # App entry, lifespan, middleware, router mounts
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── .env.example
│   ├── routers/
│   │   ├── live.py                   # /wooagent/stream — Gemini Live A2A relay (active)
│   │   ├── greet.py                  # /greet — multilingual greeting
│   │   ├── health.py                 # /health
│   │   ├── chat.py                   # /chat — DISABLED (410 Gone)
│   │   └── transcribe.py             # /transcribe — LEGACY
│   ├── services/
│   │   ├── woocommerce.py            # WC REST API client (3-tier fallback, 1300+ lines)
│   │   ├── wc_cache.py               # Redis write-through cache proxy
│   │   ├── session.py                # Session store (Redis + in-memory fallback)
│   │   ├── session_facts.py          # Structured fact extraction
│   │   ├── security.py               # HMAC validation, input sanitization
│   │   ├── rate_limit.py             # SlowAPI rate limiting
│   │   ├── llm_router.py             # 4-way hybrid LLM router (inactive/rollback)
│   │   ├── llm_clients.py            # Groq/OpenAI/Gemini SDK init (inactive/rollback)
│   │   ├── stt.py                    # Groq Whisper / Deepgram (inactive/rollback)
│   │   ├── tts.py / tts_service_v2.py  # Google TTS / ElevenLabs (inactive/rollback)
│   │   └── beta_logger.py            # Optional PostgreSQL event logging
│   ├── agent/
│   │   ├── orchestrator.py           # AgentOrchestrator + address FSM (inactive/rollback)
│   │   ├── tools.py                  # Tool execution wrappers
│   │   ├── language.py               # Language detection + speech formatting
│   │   ├── extractor.py              # Intent/entity extraction
│   │   └── prompts.py                # System prompt templates
│   ├── models/schemas.py             # Pydantic request/response schemas
│   └── tests/
│       ├── test_agent.py
│       └── test_comprehensive.py
├── docker-compose.yml                # backend + Redis stack
└── README.md
```

---

## API Endpoints

### Active

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Redis + WooCommerce connection status |
| `POST` | `/greet` | Multilingual greeting with cart context |
| `GET` | `/wooagent/ws-token` | Issue 120-second HMAC WebSocket token |
| `WS` | `/wooagent/stream` | Gemini Live A2A relay (audio + tool calls) |

All routers are also mounted under `/api/v1/` for backward compatibility.

### WebSocket Protocol (`/wooagent/stream`)

**Browser → Backend:**
- Binary frames: PCM Int16 16 kHz mono audio chunks
- JSON frames: `{"type":"text_input","text":"..."}`

**Backend → Browser:**
- Binary frames: PCM 16-bit 24 kHz mono audio from Gemini TTS
- JSON frames:
  - `{"type":"ui_action","action":{"type":"show_products","payload":{...}}}`
  - `{"type":"flush_audio","reason":"user_interrupted"}` — barge-in signal

**Connection flow:**
1. Browser fetches `GET /wooagent/ws-token?session_id=X` → HMAC token (120 s TTL)
2. Browser opens `WS /wooagent/stream?session_id=X&token=TOKEN`
3. Backend validates token, injects prior session context (cart, history, address state)
4. Full-duplex tasks run concurrently: audio relay in, audio relay out, tool execution

### Disabled

| Method | Path | Status |
|--------|------|--------|
| `POST` | `/chat` | `410 Gone` |
| `POST` | `/transcribe` | Legacy, not used |

---

## WooCommerce Tools (Gemini function declarations)

Gemini calls these during conversation; the backend executes them against the WC API:

| Tool | Description |
|------|-------------|
| `search_products` | Keyword + category + price + stock filter search |
| `get_product_details` | Full product info by ID |
| `find_variants` | Product variations list |
| `check_inventory` | Stock for product/variation |
| `compare_products` | Side-by-side comparison of two products |
| `get_categories` | Store category tree |
| `get_cart` | Current session cart |
| `add_to_cart` | Add product/variation |
| `remove_from_cart` | Remove by cart item key |
| `update_cart_quantity` | Change quantity |
| `apply_coupon` | Apply coupon code |
| `get_best_coupon` | Suggest best coupon for cart total |
| `get_orders` | Order history by customer email |
| `submit_review` | Post product review |
| `get_store_info` | Policies, shipping, payment methods |

---

## WooCommerce API Client — 3-Tier Fallback

`services/woocommerce.py` tries endpoints in order until one succeeds:

1. **Plugin REST API** (`/wp-json/wooagent/v1/...`) — most reliable, no auth issues
2. **WooCommerce Store API** (`/wp-json/wc/store/v1/...`) — public, no key required
3. **WC REST v3** (`/wp-json/wc/v3/...`) — authenticated, may be blocked on shared hosts

Results are cached in Redis with TTLs of 300–1800 s. Write operations (cart, review) bypass the cache.

---

## Session Management

`services/session.py` stores per-session state in Redis (TTL: 7200 s) with automatic in-memory fallback (up to 2000 sessions, LRU eviction):

```json
{
  "conversation_history": [{"role": "...", "content": "..."}],
  "cart_snapshot": {},
  "customer_email": "user@example.com",
  "last_products": [],
  "meta": {
    "language": "hi",
    "address_state": "idle",
    "greeted": true
  }
}
```

---

## WordPress Plugin REST Endpoints

Registered under `/wp-json/wooagent/v1/`:

| Route | Method | Auth |
|-------|--------|------|
| `/products/search` | GET | Session ID + rate limit |
| `/products/{id}` | GET | Session ID |
| `/products/{id}/variations` | GET | Session ID |
| `/products/{id}/review` | POST | HMAC or WP nonce |
| `/cart` | GET | HMAC or WP nonce |
| `/cart/add` | POST | HMAC or WP nonce |
| `/cart/remove` | POST | HMAC or WP nonce |
| `/cart/update` | POST | HMAC or WP nonce |
| `/orders/{email}` | GET | HMAC or WP nonce |
| `/session/{id}` | GET | HMAC or WP nonce |

**HMAC signature format:**
```
x-wooagent-signature: HMAC-SHA256("{timestamp}.{path}.{body}", SHARED_SECRET)
x-wooagent-timestamp: Unix timestamp (300 s window)
```

---

## Local Development Setup

### 1. Configure and start the stack

```bash
cp wooagent-backend/.env.example wooagent-backend/.env
# edit wooagent-backend/.env — at minimum set GEMINI_API_KEY, WOOCOMMERCE_*, SHARED_SECRET

docker compose up --build
```

Health check:

```bash
curl http://localhost:8000/health
```

### 2. Run backend without Docker

```bash
cd wooagent-backend
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Python 3.11 is required (Docker image: `python:3.11-slim`). Python 3.13 can break compiled dependencies.

### 3. Run without Redis

Session service has in-memory fallback — set mock mode while wiring up:

```env
MOCK_SERVICES=true
REDIS_URL=redis://localhost:6379
```

---

## WordPress Plugin Installation

1. Copy the `wooagent` folder to `wp-content/plugins/`.
2. Activate **WooAgent - AI Shopping Assistant** in WordPress admin.
3. Ensure WooCommerce is active.
4. Go to **WooCommerce → WooAgent** and configure:
   - **Agent Backend URL** — `http://localhost:8000` or your production URL
   - **API Secret Key** — must match `SHARED_SECRET` in backend `.env`
   - Widget position, color, greeting message, voice/text toggles
5. Click **Test Connection** to verify `/health`.

### Expose local backend to a live WordPress site

```bash
ngrok http 8000
# Use the HTTPS ngrok URL as Agent Backend URL in plugin settings
```

Never use `0.0.0.0` in plugin settings.

---

## WooCommerce API Key Setup

1. **WooCommerce → Settings → Advanced → REST API → Add key**
2. Description: `WooAgent Backend`, User: admin, Permissions: **Read/Write**
3. Copy the generated keys:
   - Consumer Key → `WOOCOMMERCE_CONSUMER_KEY`
   - Consumer Secret → `WOOCOMMERCE_CONSUMER_SECRET`

---

## Docker Deployment

```bash
# Stack (backend + Redis)
docker compose up -d

# Backend only
cd wooagent-backend
docker build -t wooagent-backend:latest .
docker run -d --name wooagent-backend --env-file .env -p 8000:8000 wooagent-backend:latest
```

---

## Environment Variable Reference

### Required

| Variable | Description |
|----------|-------------|
| `GEMINI_API_KEY` | Google Gemini API key (Gemini Live A2A) |
| `WOOCOMMERCE_STORE_URL` | WordPress store base URL |
| `WOOCOMMERCE_CONSUMER_KEY` | WooCommerce REST API consumer key |
| `WOOCOMMERCE_CONSUMER_SECRET` | WooCommerce REST API consumer secret |
| `SHARED_SECRET` | HMAC secret — must match WordPress plugin setting |
| `REDIS_URL` | Redis connection URL (e.g. `redis://redis:6379`) |
| `ALLOWED_ORIGINS` | Comma-separated CORS origins (scheme + host, no trailing slash) |

### Optional — Store Branding

| Variable | Description |
|----------|-------------|
| `STORE_NAME` | Used in system prompt |
| `STORE_CURRENCY` | Currency symbol (e.g. `₹`) |
| `STORE_ABOUT` | Short store description for the assistant |
| `STORE_SHIPPING_POLICY` | Injected into assistant knowledge |
| `STORE_RETURNS_POLICY` | Injected into assistant knowledge |
| `STORE_PAYMENT_METHODS` | Injected into assistant knowledge |

### Optional — Fallback LLM (inactive by default)

| Variable | Description |
|----------|-------------|
| `GROQ_API_KEY` | Groq API key (legacy HTTP chat path) |
| `GROQ_MODEL` | e.g. `llama-3.3-70b-versatile` |
| `GROQ_STT_MODEL` | e.g. `whisper-large-v3-turbo` |
| `OPENAI_API_KEY` | OpenAI API key (legacy routing) |

### Optional — TTS Fallback (inactive by default)

| Variable | Description |
|----------|-------------|
| `TTS_PROVIDER` | `google`, `elevenlabs`, or `browser` |
| `GOOGLE_TTS_API_KEY` | Google Cloud TTS key |
| `ELEVENLABS_API_KEY` | ElevenLabs API key |

### Optional — Features & Infra

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8000` | Backend port |
| `LOG_LEVEL` | `INFO` | Uvicorn log level |
| `MOCK_SERVICES` | `false` | In-memory Redis fallback for local dev |
| `MVP_MODE` | `true` | Disables billing/tier enforcement |
| `BETA_LOGGING_ENABLED` | `false` | PostgreSQL event logging |
| `DATABASE_URL` | — | PostgreSQL URL for beta logging |
| `WOOCOMMERCE_AUTH_METHOD` | `auto` | `auto`, `v3`, or `store_api` |
| `WOOCOMMERCE_ENABLE_WC_V3_FALLBACK` | `false` | Enable WC v3 REST fallback |

---

## Running Tests

```bash
cd wooagent-backend
pytest -q
```

Coverage:
- Product search tool flow
- Add-to-cart tool flow
- Session persistence flow

---

## Troubleshooting

| Symptom | Cause & Fix |
|---------|-------------|
| `HTTP 403: Cookie check failed` | Use latest plugin build — widget sends `x-wooagent-nonce` header, not WP cookie auth |
| `HTTP 502 cURL error 28 timeout` | Backend URL unreachable from WordPress: wrong host, dead ngrok tunnel, blocked port, or SSL mismatch |
| Repeated `"Temporary connectivity issue"` | CORS error — check browser console; `ALLOWED_ORIGINS` must match exact origin (scheme + host, no path) |
| WebSocket closes immediately | `SHARED_SECRET` mismatch between plugin and backend, or expired ws-token (120 s TTL) |
| No audio from assistant | Browser must be served over HTTPS for AudioWorklet; check `GEMINI_API_KEY` validity |
| Redis connection errors at startup | Backend falls back to in-memory sessions automatically — no action needed for dev |
>>>>>>> 0a8e249 (Initial commit: Agentic Commerce project foundation)
