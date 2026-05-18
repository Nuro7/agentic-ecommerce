# Speako — AI Shopping Assistant for Shopify & WooCommerce

Speako is a multi-tenant SaaS platform that embeds an AI-powered chat widget ("Aria") into any Shopify or WooCommerce store. Customers can search products, add to cart, apply coupons, and complete checkout — all through voice or text conversation.

---

## What it does

- **AI Shopping Assistant** — Aria searches products, adds items to cart, applies discounts, and guides checkout via natural conversation
- **Voice + Text** — Full voice support via Gemini Live WebSocket; text fallback always available
- **Multi-platform** — Works on Shopify (script tag injection) and WooCommerce (WordPress plugin)
- **Multi-tenant SaaS** — One backend serves unlimited stores; each merchant gets isolated data
- **Multilingual** — English, Hindi, Malayalam, Tamil, Telugu, Bengali, Kannada, Gujarati, Punjabi

---

## Project structure

```
/
├── backend/                    ← FastAPI backend (single source of truth)
│   ├── src/app/
│   │   ├── server.py           App factory + lifespan
│   │   ├── config.py           Settings (pydantic-settings)
│   │   ├── agent/              AI agent — LLM routing, tools, voice, memory
│   │   ├── api/v1/             REST + WebSocket endpoints
│   │   ├── integrations/       Shopify, WooCommerce, Custom API clients
│   │   └── modules/            Multi-tenant domain — tenants, auth, billing, etc.
│   ├── static/                 Widget JS + CSS (served at /static/)
│   ├── migrations/             Alembic migrations
│   └── pyproject.toml
│
├── plugins/
│   └── wordpress/wooagent/     WordPress plugin (WooCommerce integration)
│
└── infra/
    └── docker/                 Dockerfiles + Compose files
```

---

## Quick start (local dev)

### Prerequisites
- Docker Desktop running
- ngrok installed (for Shopify testing)

### 1. Clone and configure

```bash
git clone https://github.com/YOUR-USERNAME/agentic-ecom.git
cd agentic-ecom
cp backend/.env.example backend/.env
# Edit backend/.env with your API keys
```

### 2. Start the stack

```bash
docker compose -f infra/docker/docker-compose.dev.yml up -d
```

This starts:
- **PostgreSQL** on `localhost:5432`
- **Redis** on `localhost:6379`
- **FastAPI app** on `localhost:8000` (hot reload enabled)

### 3. Run migrations

```bash
docker compose -f infra/docker/docker-compose.dev.yml exec app alembic upgrade head
```

### 4. Verify

```bash
curl http://localhost:8000/api/v1/health
# → {"status":"ok","redis":true}
```

API docs: http://localhost:8000/docs

---

## Shopify setup

### Testing (ngrok)

```bash
# Start ngrok
ngrok http 8000

# Register widget on your store (replace with your ngrok URL)
curl -X POST http://localhost:8000/api/v1/shopify/setup \
  -H "Content-Type: application/json" \
  -d '{"backend_url": "https://YOUR-NGROK-URL.ngrok-free.app"}'
```

### Production (Railway / any server)

```bash
# After deploying backend, register once:
curl -X POST https://your-backend.railway.app/api/v1/shopify/setup \
  -H "Content-Type: application/json" \
  -d '{"backend_url": "https://your-backend.railway.app"}'
```

### OAuth (SaaS — any merchant can install)

Set these in your Shopify Partner app configuration:

| Field | Value |
|-------|-------|
| App URL | `https://your-backend.railway.app/api/v1/shopify/install` |
| Redirect URL | `https://your-backend.railway.app/api/v1/shopify/callback` |

Merchant install link:
```
https://your-backend.railway.app/api/v1/shopify/install?shop=MERCHANT-STORE.myshopify.com
```

---

## WooCommerce setup

1. Upload `plugins/wordpress/wooagent/` to your WordPress site
2. Activate the **WooAgent** plugin
3. Go to **WooAgent → Settings** and set your backend URL
4. The widget appears automatically on all pages

---

## Environment variables

| Variable | Description |
|----------|-------------|
| `PLATFORM` | `shopify` or `woocommerce` |
| `SHOPIFY_STORE_DOMAIN` | e.g. `mystore.myshopify.com` |
| `SHOPIFY_STOREFRONT_TOKEN` | Shopify Storefront API token |
| `SHOPIFY_ADMIN_TOKEN` | Shopify Admin API token |
| `SHOPIFY_API_KEY` | Partner app API key (OAuth) |
| `SHOPIFY_API_SECRET` | Partner app API secret (OAuth) |
| `WOOCOMMERCE_STORE_URL` | WordPress site URL |
| `WOOCOMMERCE_CONSUMER_KEY` | WC REST API key |
| `WOOCOMMERCE_CONSUMER_SECRET` | WC REST API secret |
| `OPENAI_API_KEY` | GPT-4o / GPT-4o-mini |
| `GROQ_API_KEY` | STT (Whisper) + LLaMA |
| `GEMINI_API_KEY` | Gemini Live (voice) |
| `GOOGLE_TTS_API_KEY` | Google Cloud TTS |
| `DATABASE_URL` | PostgreSQL async URL |
| `REDIS_URL` | Redis URL |
| `JWT_SECRET_KEY` | JWT signing key |
| `BACKEND_URL` | Public backend URL (ngrok or production) |

---

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/health` | Health check |
| `POST` | `/api/v1/greet` | Widget open greeting |
| `GET` | `/api/v1/cart` | Get cart (public) |
| `WS` | `/wooagent/stream` | Gemini Live voice stream |
| `GET` | `/api/v1/shopify/install` | OAuth install (redirect to Shopify) |
| `GET` | `/api/v1/shopify/callback` | OAuth callback |
| `POST` | `/api/v1/shopify/setup` | Manual script tag registration |
| `GET` | `/api/v1/shopify/widget-loader.js` | Dynamic widget loader |
| `GET` | `/static/wooagent-widget.js` | Widget JS |

---

## Tech stack

| Layer | Technology |
|-------|-----------|
| Backend | FastAPI, Python 3.12, SQLAlchemy 2.0 (async) |
| Database | PostgreSQL (asyncpg) |
| Cache | Redis |
| AI | GPT-4o, Groq LLaMA, Gemini Live |
| Voice STT | Groq Whisper |
| Voice TTS | Google Cloud TTS, ElevenLabs |
| Auth | PyJWT, Argon2 |
| Deploy | Docker, Railway |

---

## Useful commands

```bash
# View live logs
docker compose -f infra/docker/docker-compose.dev.yml logs -f app

# Restart app after config change
docker compose -f infra/docker/docker-compose.dev.yml up -d app

# Stop everything
docker compose -f infra/docker/docker-compose.dev.yml down

# Run migrations
docker compose -f infra/docker/docker-compose.dev.yml exec app alembic upgrade head
```

---

## License

MIT
