# Speako — Local Integration Testing (Custom Platform)

Complete, verified procedure to test the full Speako custom-platform integration
on your machine using the included **test store** (`test_store.py`). Every step
below was run and confirmed working.

The test store fakes a custom ecommerce backend: it implements all the REST
endpoints Speako calls, with 5 sample products and per-session carts. No
dependencies — Python 3 standard library only.

---

## What gets tested

```
 Aria widget ──▶ Speako backend (Docker, :8000) ──▶ test_store.py (host, :9000)
                        │                                   ▲
                        └──── product sync + cart calls ────┘
```

1. Speako reaches your store API  → `test-connection`
2. Merchant self-registers        → `onboard` → snippet + tenant id
3. Speako syncs your catalog       → products cached
4. Widget renders + Aria answers   → end-to-end

---

## Prerequisites (one-time)

- **Docker Desktop running.**
- `ENVIRONMENT=dev` in `backend/.env` (already set). This activates the dev-only
  SSRF bypass that lets Speako call a `localhost`/private test store. **In
  production the SSRF guard stays fully enforced** — see "How local testing is
  enabled" at the bottom.

---

## Step 1 — Start the Speako backend (all 5 services)

```bash
docker compose -f infra/docker/docker-compose.dev.yml up -d
```

Confirm health:

```bash
curl http://localhost:8000/api/v1/health      # → {"status":"ok","redis":true}
```

Confirm all containers are up (app **and** worker **and** beat — the worker is
required for product sync):

```bash
docker ps --format "{{.Names}}\t{{.Status}}" | findstr docker-
# expect: docker-app-1, docker-worker-1, docker-beat-1, docker-redis-1, docker-postgres-1
```

> If `docker-worker-1` / `docker-beat-1` are missing, product sync will silently
> not run. Re-run the `up -d` command above. (Containers exiting with code 137 =
> Docker out of memory — raise Docker Desktop's memory to 4 GB+.)

---

## Step 2 — Start the test store (in a separate terminal)

```bash
cd test_store
python test_store.py
```

You should see:

```
Speako test store listening on http://0.0.0.0:9000
API key: test-key-123  (send as 'Authorization: Bearer test-key-123')
5 sample products loaded. Ctrl+C to stop.
```

Quick self-check:

```bash
curl http://localhost:9000/health                                   # {"status":"ok"}
curl -H "Authorization: Bearer test-key-123" http://localhost:9000/products/search
```

---

## Step 3 — Confirm Speako (in Docker) can reach the test store

The Speako container reaches your host via `host.docker.internal`. So the store
base URL Speako must use is **`http://host.docker.internal:9000`** (NOT
`localhost:9000`, which inside the container means the container itself).

```bash
curl -s -X POST http://localhost:8000/api/v1/onboard/test-connection ^
  -H "Content-Type: application/json" ^
  -d "{\"platform\":\"custom_api\",\"custom_api_base_url\":\"http://host.docker.internal:9000\",\"custom_api_key\":\"test-key-123\"}"
```

Expected (verified):

```json
{"ok":true,"platform":"custom_api","products_found":1,
 "message":"Connection successful. Found 1 product(s)."}
```

You can also do this via the UI: open `http://localhost:8000/static/onboard.html`,
choose **Custom platform**, enter base URL `http://host.docker.internal:9000` and
API key `test-key-123`, and click **Test connection**.

---

## Step 4 — Onboard the test store

Via the page (recommended): on `onboard.html`, fill store name + a **real-looking
email** (the `.test` / `.local` TLDs are rejected by the email validator — use
e.g. `merchant@speakotest.com`), a password, then **Create account**.

Or via curl (verified):

```bash
curl -s -X POST http://localhost:8000/api/v1/onboard/ ^
  -H "Content-Type: application/json" ^
  -d "{\"store_name\":\"Speako Test Store\",\"email\":\"merchant@speakotest.com\",\"password\":\"test1234\",\"platform\":\"custom_api\",\"custom_api_base_url\":\"http://host.docker.internal:9000\",\"custom_api_key\":\"test-key-123\"}"
```

Response includes your `tenant_id` and the ready-to-paste `widget_snippet`.
Copy the `tenant_id`.

---

## Step 5 — Confirm the product catalog synced

On onboarding, Speako queues a background sync (needs the worker running). Within
~10s, all 5 products should be cached. Verify:

```bash
docker compose -f infra/docker/docker-compose.dev.yml exec postgres ^
  psql -U agentic -d agentic_commerce -c ^
  "SELECT count(*) FROM product_cache WHERE tenant_id='<YOUR_TENANT_ID>';"
# expect: 5
```

If it shows 0 (e.g. worker was down at onboard time), trigger the sync manually:

```bash
docker exec -i docker-app-1 python -c "from src.app.workers.tasks.sync_products import sync_products; print(sync_products.apply(kwargs={'tenant_id':'<YOUR_TENANT_ID>'}).get())"
# expect: {'tenants': 1, 'upserted': 5, 'skipped': 0}
```

---

## Step 6 — Render the widget and test Aria

Paste the snippet (just before `</body>`) into any local HTML page, or into the
demo storefront if you have one. For a quick test, save this as `demo.html` and
open it in a browser:

```html
<!DOCTYPE html><html><body>
  <h1>Test storefront</h1>

  <!-- Speako snippet from Step 4 -->
  <script>
    window.wooagent_config = {
      backend_url: "http://localhost:8000",
      tenant_id:   "<YOUR_TENANT_ID>",
      store_name:  "Speako Test Store"
    };
  </script>
  <script src="http://localhost:8000/static/wooagent-widget.js" async></script>
</body></html>
```

The **Aria chat bubble** appears bottom-right. Test:

- *"Show me your products"* → should list the synced items.
- *"Add the blue t-shirt to my cart"* → Speako calls your test store's
  `/cart/add`; watch the test-store terminal log the `POST /cart/add` and the
  cart update.

> `backend_url: "http://localhost:8000"` works because you open `demo.html` on the
> same machine. For a real external site you'd use a public Speako URL.

---

## Endpoints the test store implements

| Method & path | Required by Speako | Notes |
|---|---|---|
| `GET /products/search` | yes | params `q`, `limit`, `in_stock_only`, `category` |
| `GET /products/{id}` | yes | one product |
| `GET /products` | sync | bulk catalog fetch |
| `GET /cart?session_id=` | yes | per-session cart |
| `POST /cart/add` | yes | `{session_id, product_id, quantity}` |
| `POST /cart/remove` | yes | `{session_id, product_id}` |
| `PUT /cart/update` | yes | `{session_id, product_id, quantity}` |
| `GET /products/{id}/inventory` | optional | |
| `GET /products/{id}/variations` | optional | returns `[]` |
| `GET /categories` | optional | |
| `GET /store/info` `GET /store/policies` | optional | |
| `GET /health` | n/a | no-auth liveness probe |

All except `/health` require `Authorization: Bearer test-key-123`.

---

## How local testing is enabled (and why it's safe)

Speako's SSRF guard ([backend/src/app/core/net.py](../backend/src/app/core/net.py))
normally rejects `localhost`/private store URLs so a merchant can't point Speako
at internal services. To allow a local test store, a **dev-only bypass** was added
in [backend/src/app/api/v1/onboarding.py](../backend/src/app/api/v1/onboarding.py):

```python
# Dev only: allow localhost / private store URLs ...
if settings.environment.lower() in ("dev", "development", "local"):
    return
```

This runs **only when `ENVIRONMENT=dev`**. With `ENVIRONMENT=production`, the full
SSRF validation applies and private URLs are rejected — so this changes nothing
about production safety.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ERR_CONNECTION_REFUSED` on :8000 | app container down → `docker compose -f infra/docker/docker-compose.dev.yml up -d app` |
| test-connection: "non-public address" | `ENVIRONMENT` isn't `dev`, or you used `localhost:9000` instead of `host.docker.internal:9000` |
| test-connection fails to connect | test store not running, or wrong port/key |
| `product_cache` count = 0 | worker was down at onboard → start it, then run the manual sync (Step 5) |
| email rejected on signup | don't use `.test`/`.local` domains; use e.g. `@speakotest.com` |
| containers exit code 137 | Docker out of memory → raise Docker Desktop memory limit |
