# Speako — Production Deployment Checklist

Actionable runbook for shipping the hardened backend. Ordered by gate. Do **not**
onboard a real merchant until **Gate A** is fully checked.

The code-level fixes (auth, webhook HMAC, SSRF, sync pagination, rate limiting,
circuit breakers, atomic quota, encryption-at-rest, observability, migrations)
are already implemented. The steps below are the **operational** work only you can
do, plus validation.

---

## Gate A — security & worker tier (blocks first real merchant)

### A1. Rotate every leaked credential (treat all as compromised)
The following were committed to `backend/.env` / `wooagent-backend/.env` — rotate
at the provider, then update the production secret store (NOT a committed file):
- [ ] `SHOPIFY_ADMIN_TOKEN` (shpat_…) and `SHOPIFY_API_SECRET` (shpss_…)
- [ ] `OPENAI_API_KEY`, `GROK_API_KEY` (xAI), `GEMINI_API_KEY`
- [ ] `GOOGLE_TTS_API_KEY`, `ELEVENLABS_API_KEY`
- [ ] `WOOCOMMERCE_CONSUMER_KEY` / `WOOCOMMERCE_CONSUMER_SECRET`
- [ ] `NGROK_AUTHTOKEN`
- [ ] Confirm old keys are **revoked** (not just rotated).

### A2. Generate strong app secrets (a startup guard refuses placeholders in prod)
- [ ] `JWT_SECRET_KEY` — `python -c "import secrets; print(secrets.token_urlsafe(48))"`
- [ ] `SHARED_SECRET` — a strong random value (not `nif@123`)
- [ ] `ENCRYPTION_KEY` — `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
      (until set, credential encryption is a safe no-op; new writes encrypt once set)
- [ ] Ensure `ENVIRONMENT=production` and `DEBUG=false` (the guard in `config.py`
      will refuse to boot otherwise).

### A3. Get `.env` out of the repo
- [ ] `.gitignore` already covers `.env`; confirm no `.env` is tracked. If this
      ever becomes a git repo, purge `.env` from history before pushing.
- [ ] Move all secrets to the platform secret store (Render Env Group / vault).

### A4. Run database migrations
- [ ] `alembic upgrade head` (applies `0010` password+indexes+conversation
      constraint, `0011` HNSW vector index).
- [ ] On a large existing `product_cache`, build the HNSW index with
      `CREATE INDEX CONCURRENTLY` manually instead of in-migration (see `0011` note).

### A5. Deploy the background worker tier (required — not just the web service!)
Without these, retries / webhooks / billing / analytics / scheduled product sync
never run, and searches fall back to the live store API. The `render.yaml` Blueprint
already declares all three services:
- [ ] `speako-web`   — uvicorn (health-checked, public).
- [ ] `speako-worker` — `celery -A src.app.workers.celery_app worker -l info`
- [ ] `speako-beat`   — `celery -A src.app.workers.celery_app beat -l info`
- [ ] All three share the `speako` Render Env Group (`DATABASE_URL` + `REDIS_URL` + keys).
- [ ] Migrations run via the web service `preDeployCommand: alembic upgrade head`.
- [ ] (Locally, worker + beat run via docker-compose automatically.)

### A6. Verify Gate A
- [ ] `make test` (in `backend/`) is green — auth-isolation, login, webhook,
      rate-limit tests pass.
- [ ] Cross-tenant probe: tenant A's JWT on `/api/v1/orders/{B}` → 401/403/404.
- [ ] `POST /api/v1/webhooks/shopify/{tid}` with bad/missing HMAC → 401.
- [ ] `GET /api/v1/ops` shows redis up and (after enqueuing a webhook) the
      `webhooks_pending` count returning to 0 within a beat cycle.

---

## Gate B — cost & reliability (before ~10 stores)

- [ ] Raise `DATABASE_POOL_SIZE` for production (currently `5` in `.env`; e.g. 20).
- [ ] Load test `/greet` concurrently → expect 429s; assert recorded usage never
      exceeds the plan limit (atomic quota).
- [ ] Degradation drills: kill Redis → app still serves via in-memory session;
      kill the primary LLM provider → failover + breaker opens after 3 failures.
- [ ] Catalog scale: seed a 1,000-product store; run `sync_products`; assert
      `product_cache` count ≈ source and deep-catalog items are searchable.

---

## Gate C — topology & observability (before 100 stores)

- [ ] Provision a **dedicated broker Redis** (set `CELERY_BROKER_URL`); keep
      sessions on a Redis with a **no-evict** memory policy.
- [ ] Run web with `uvicorn --workers N`; move reranker/embeddings off the event
      loop (thread pool or push to the Celery tier).
- [ ] Wire `/api/v1/ops` metrics into alerting (queue depth, dead-letter size,
      pending-webhook age, per-tenant LLM/voice spend).

---

## Known follow-ups (tracked, not blocking)
- Remove `wooagent-backend/` (deprecated second backend with its own secrets).
- Encrypt `custom_api_key` properly (needs a deterministic lookup-hash column —
  it's currently plaintext because it doubles as the `/ingest` lookup key).
- Webhook idempotency unique constraint + content-hash re-embed.
- Refactors (after tests are green): `execute_tool_call` dispatch table, split
  `ask_brain`, merge `TTSService`/`TTSServiceV2`.

Full findings + architecture ADRs: see the session plan document.
