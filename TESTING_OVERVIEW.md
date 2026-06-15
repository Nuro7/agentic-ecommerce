# Speako — Complete Testing Overview

Authoritative record of the QA work performed on the Speako multi-tenant AI
shopping-assistant backend: **what was tested, how it was tested, the evidence,
and every finding.** Covers two efforts:

- **Part A — Functional multi-merchant campaign** (3 simulated merchants, real Aria conversations).
- **Part B — Scale test** (40 merchants × 1000 products = 40,000 products).

Companion files: [TEST_REPORT.md](TEST_REPORT.md) (campaign summary), harnesses in
`test_store/`.

---

## 1. Scope & honesty statement

**Tested (real, not mocked):** onboarding, multi-tenant data isolation, product
sync at scale, cached search, carts, auth/IDOR, quota, rate-limiting, webhooks,
edge/abuse inputs, and **live AI conversations** with Aria (Grok/GPT/Gemini +
Google TTS were all live).

**NOT testable with available tooling (stated plainly):**
- Real **voice audio** — no microphone/STT capture, no audio playback, no barge-in.
- **Visual widget UI** — no browser automation; the rendered widget, layout, and
  CSS were not exercised. Everything beneath the UI (APIs, agent logic, tools)
  was.
- **Live Shopify OAuth install** (needs a real merchant redirect). The custom-API
  path was tested end-to-end instead.

---

## 2. Test environment & infrastructure

| Component | Detail |
|---|---|
| Stack | Docker Compose dev: `app` (FastAPI :8000), `worker` + `beat` (Celery), `postgres` (:5432), `redis` (:6379) |
| Memory ceiling | Docker VM = **3.5 GiB total** (relevant to the OOM findings) |
| Mode | `ENVIRONMENT=dev`, `MVP_MODE=true` (WS token not required) |
| LLMs (live) | Grok (primary), GPT-4o/4o-mini, Gemini brain, Google TTS — all keys valid |
| Merchant simulation | `custom_api` tenants backed by local **test stores** (Python stdlib HTTP servers) reachable from containers via `host.docker.internal` |
| Dev SSRF bypass | The earlier-added dev-only bypass lets onboarding accept `host.docker.internal` URLs (prod still enforces SSRF) |

**Test stores written for this:**
- `test_store/test_store.py` — small fixed catalogs, 3 variants (alpha/beta/gamma, distinct IDs).
- `test_store/test_store_scale.py` — generates a **distinct 1000-product catalog per merchant**, keyed by `Bearer key-m-{N}`, with disjoint ID ranges (`N*1_000_000+1..+1000`) so isolation is trivially verifiable. Supports pagination.

**Harnesses (drive the running stack as a black box + in-process checks):**
- `test_store/run_test_campaign.py` — the functional campaign (Part A).
- `test_store/run_scale_test.py` — the scale test (Part B).

---

## 3. Methodology — how each layer was driven

| Layer | How it was exercised |
|---|---|
| REST endpoints | `httpx` calls to `/api/v1/...` (onboard, greet, cart, auth/login, tenants, webhooks) |
| Tenant routing | `X-Tenant-ID` header (widget endpoints) and JWT (merchant endpoints) |
| AI agent (Aria) | **Voice WebSocket** `/wooagent/stream` with `{"type":"text_input",...}` (pure text, no audio), passing `tenant_id` so the per-tenant store client is used |
| Product sync | Celery task auto-queued on onboard, plus explicit `_sync_async()` run + measured |
| Search | `hybrid_search()` + `bm25_search()`/`vector_search()` called directly; verified against raw SQL |
| Data assertions | Direct SQL against Postgres `product_cache` / `tenants` |
| Quota | Seeded usage in-process via `BillingService`, then hit the HTTP path expecting 402 |
| Observability | `docker stats` for memory, `docker logs` for tracebacks/fallbacks |

---

## 4. Part A — Functional multi-merchant campaign

**Setup:** 3 merchants (Alpha=apparel, Beta=electronics, Gamma=home), each a
`custom_api` tenant on its own test store. Result: **32 PASS · 1 WARN · 2 FAIL.**

### Scenarios, method, and result

| # | Area | How tested | Result |
|---|---|---|---|
| 1 | Onboard 3 merchants | `POST /onboard/` | PASS (201 + tenant_id each) |
| 2 | Duplicate email | re-onboard same email | PASS (409) |
| 3 | Missing required field | onboard without `custom_api_base_url` | PASS (422) |
| 4 | Invalid platform | `platform=magento` | PASS (422) |
| 5 | Malformed JSON | bad body to `/onboard/` | PASS (422) |
| 6 | test-connection (reachable) | valid store URL | PASS (ok:true, found 1) |
| 7 | test-connection (unreachable) | dead port URL | **FAIL** — returns ok:true (bug #2) |
| 8 | Product sync per merchant | `_sync_async(tenant)` + count | PASS (5/5/4 exact) |
| 9 | Cross-tenant product isolation | SQL: alpha∩beta platform_ids | PASS (∅) |
| 10 | Cart routing isolation | add to alpha store; read `/cart` w/ alpha vs beta header | PASS (alpha=1, beta=0) |
| 11 | Auth login + `/tenants/me` | `POST /auth/login` → JWT | PASS |
| 12 | **IDOR** | alpha JWT on beta's `/tenants/{id}` | PASS (403) |
| 13 | Quota | seed over limit → `/greet` | PASS (402) |
| 14 | Rate limit | burst `/cart` | PASS (429s) |
| 15 | Webhook bad/missing HMAC | wrong/no signature | PASS (401/401) |
| 16 | Webhook valid HMAC | correct signature, partial product | **FAIL** — 500 (bug #1) |
| 17 | Voice WS missing/short session_id | connect w/o session_id | PASS (rejected — validates earlier fix) |
| 18 | Concurrent session writes | 5 concurrent `/greet`, same session | PASS (no 5xx — validates session-lock fix) |
| 19 | Voice gate (Starter) | WS as Starter plan | Documented — blocked (finding #3) |
| 20 | **Agent: off-topic guard** | "what's the weather?" | PASS — refused: "I'm here to help you shop." |
| 21 | **Agent: hallucination guard** | "purple dragon costume XXL?" | PASS — no fabrication, asks to check live stock |
| 22 | **Agent: multilingual** | Hindi query | PASS — replied in Hindi (Devanagari) |
| 23 | **Agent: per-merchant catalog** | beta "show earbuds" / alpha products | PASS — beta=electronics, alpha=apparel, no leak |
| 24 | Agent: add-to-cart | "add the blue cotton t-shirt" | WARN (finding #5 — emits `show_cart`, cart-content not independently verified) |

---

## 5. Part B — Scale test (40 merchants × 1000 products)

**Setup:** one `test_store_scale.py` instance; 40 `custom_api` merchants, each a
distinct 1000-product catalog (disjoint ID ranges). Full-send (no ramp).

### Measured results

| Metric | Result |
|---|---|
| Onboarding | **40/40 merchants in 9.2 s**, 0 failures |
| Products synced | **40,000** (every merchant exactly 1000, **0 short**) — verified by SQL |
| Tenant isolation at scale | **0 violations** — every merchant's product IDs stayed in its own range |
| Cross-tenant overlap | none |
| Memory | peak ≈ **2.4 GiB / 3.5 GiB**, **no OOM** during the run |
| Sync throughput | ~39k products cached by ~135 s (incl. OpenAI embedding of every product) |
| Embeddings / FTS | all 40,000 rows have both `embedding` and `search_vector` populated |
| Search (isolated, no load) | BM25 returns correct rows (e.g. 50 hits for "shirt"); functional |
| Search (under concurrent sync) | **degraded to 0 results** with dbapi/transaction-aborted errors (finding #4) |

### How verified
- `SELECT count(*) ... GROUP BY tenant` → all 40 at exactly 1000.
- Range check: `min/max(platform_id)` per merchant must equal `N*1e6+1 .. +1000` → 0 violators.
- `docker stats` sampled through the run for memory.
- BM25/vector functions called directly + compared to raw SQL counts.

---

## 6. Consolidated findings

### 🔴 Confirmed bugs

**#1 — Custom webhook 500 on a product missing `in_stock`** (MEDIUM-HIGH)
`webhooks/service.py:269 upsert_product` inserts `in_stock=NULL` into a NOT-NULL
column → `NotNullViolationError`. The sync path defaults missing availability to
`True` via the adapter; the webhook path doesn't. Partial webhook payloads (common
in production) crash. *Fix: default `in_stock` (and other NOT-NULL cols) in the
webhook upsert, or route it through the same canonical adapter as sync.*
**(A fix for this was drafted and is paused at your request.)**

**#2 — `test-connection` reports success for an unreachable custom store** (MEDIUM)
`CustomApiClient.search_products` swallows all errors and returns `[]`, so
`/onboard/test-connection` returns `{ok:true, products_found:0}` even when nothing
is listening. A merchant who typos their URL is told "Connection successful."
*Fix: do an explicit reachability probe (or treat connection errors distinctly)
and return `ok:false`.*

**#4 — DB pool exhaustion + unrecoverable search fallback under load** (MEDIUM, scale)
During the 40k concurrent sync, the connection pool (`DATABASE_POOL_SIZE=5`,
`MAX_OVERFLOW=2`) saturated and concurrent search queries failed with
`InFailedSQLTransactionError` → cached search returned **0 results** (silently
falling back to the live store API). Two parts:
- *Capacity:* pool of 5+2 is too small for sync + serving concurrently (the
  production checklist Gate B already flags raising `DATABASE_POOL_SIZE`).
- *Latent code bug:* in `hybrid_search.bm25_search`, when the tsvector query
  errors it falls back to `_ilike_search` **inside the same aborted transaction**,
  which is then guaranteed to fail too. The fallback can never recover. *Fix:
  rollback / use a SAVEPOINT before the fallback, and give the two `asyncio.gather`
  search arms independent sessions instead of sharing one connection.*
> Note: when **not** under pool contention, BM25 + vector search work correctly
> (verified in isolation). This degrades under load, not always.

### 🟠 Design / quality (confirm intent)

**#3 — Starter (free) plan can't use the assistant at all** (HIGH, confirm)
Every `/wooagent/stream` connection is unconditionally voice-gated
(`enforce_conversation_quota(is_voice=True)` + `allow_voice`); Starter has
`allow_voice:false`, so its customers get *"Voice is not available"* — even for
text. The widget's only chat path is this WS. *If Starter should have text chat,
the WS needs a text-only path that skips the voice gate.*

**#5 — "Show me your products" leads with an out-of-stock item; add-to-cart action mismatch** (MEDIUM, agent quality)
Broad "show products" returned a single **out-of-stock** SKU; add-to-cart emitted
`show_cart` rather than an explicit add (cart persistence not independently
verified). *Bias presentation toward in-stock, return several; verify the cart
op + UI action match.*

**#6 — Stale/unreachable tenant URLs stall the global sync sweep** (LOW)
A full `_sync_async()` iterates all tenants; dead store URLs from prior runs each
incur ConnectTimeout retries, slowing the sweep substantially. *Skip/short-circuit
repeatedly-unreachable tenants.*

---

## 7. What works well (verified)

Onboarding (incl. all negative/validation cases) · per-merchant product sync at
scale (40k, exact counts) · **strong multi-tenant isolation** (products, carts,
IDOR-blocked) · quota (402) · rate-limiting (429) · webhook signature verification
· abuse-input handling · **AI guardrails** (off-topic refusal, no hallucination)
· **multilingual** (Hindi) · **per-merchant agent catalog** with no cross-leak ·
no OOM at 40k · session-isolation + session-lock fixes (from the prior review)
confirmed in behavior.

---

## 8. How to reproduce

```bash
# 1. Stack up
docker compose -f infra/docker/docker-compose.dev.yml up -d

# 2a. Functional campaign — start 3 variant stores, then:
#     (alpha:9001, beta:9002, gamma:9003 via STORE_VARIANT + API_KEY)
docker exec -e RUN_ID=<unique> docker-app-1 python static/run_test_campaign.py

# 2b. Scale test — start scale store, then:
PORT=9100 PRODUCTS_PER=1000 python test_store/test_store_scale.py   # on host
docker exec -e RUN_ID=<unique> -e N_MERCHANTS=40 docker-app-1 python static/run_scale_test.py
```
(The harnesses live in `test_store/`; copy into a container-mounted dir such as
`backend/static/` to run via `docker exec`. Findings #1, #2 are deterministic;
agent findings depend on live LLM output; #4 appears under concurrent load.)

---

## 9. Limits & caveats
- Agent conversations cost real LLM/TTS credits — Part A used ~9 turns by design.
- Scale test embeds 40k products via OpenAI (cost ≈ negligible; time ≈ minutes).
- The 3.5 GiB Docker ceiling is tight; a larger run (≥49 merchants) risks OOM.
- Voice audio and the visual widget were not testable here.

---

## 10. Appendix A — Validation criteria (pass conditions per test)

| Test | Validation criterion (what made it PASS/FAIL) |
|---|---|
| Onboard | HTTP 201 and a `tenant_id` returned; tenant row exists in DB |
| Duplicate email | HTTP 409 (not 201/500) |
| Missing field / bad platform / bad JSON | HTTP 422 |
| test-connection reachable | `ok:true` and `products_found ≥ 1` |
| test-connection unreachable | expected `ok:false` → **got `ok:true` = FAIL** |
| Product sync | `product_cache` row count for tenant **equals** the store's catalog size |
| Product isolation | intersection of two tenants' `platform_id` sets is empty; scale: every `platform_id ∈ [N·1e6+1, N·1e6+1000]` |
| Cart routing | item visible under owning tenant's header, `item_count=0` under another tenant's header |
| IDOR | cross-tenant `GET /tenants/{id}` returns 403/404 (not 200) |
| Quota | after usage seeded over the plan limit, next call returns HTTP 402 |
| Rate limit | bursting past the window yields ≥1 HTTP 429 |
| Webhook good/bad/missing HMAC | valid → 2xx; tampered → 401; absent → 401 |
| WS missing/short session_id | connection rejected (not accepted into a shared session) |
| Concurrent session writes | no HTTP 5xx across concurrent same-session requests |
| Agent off-topic | reply does not answer the off-topic question; redirects to shopping |
| Agent hallucination | reply does not invent a non-existent product/attribute |
| Agent multilingual | reply is in the user's language/script |
| Agent catalog isolation | reply references only the queried merchant's products |
| Scale: no OOM | all containers stay `Up`; no exit 137 during the run |

## 11. Appendix B — Raw execution evidence (selected)

**Scale onboarding + sync (harness log):**
```
Onboard | 40 merchants (0 failed) -> 40 ok in 9.2s
```
**Per-merchant catalog (SQL):**
```
 merchants | total_products | min_cat | max_cat | at_1000 | short
        40 |          40000 |    1000 |    1000 |      40 |     0
```
**Isolation at scale (SQL):**
```
violating_merchants
------------------- 
                 0
```
**Memory during 40k sync (docker stats):**
```
docker-worker-1: ~1.30 GiB   docker-postgres-1: ~0.80 GiB   docker-app-1: ~0.25 GiB
peak total ≈ 2.4 GiB / 3.5 GiB  → no OOM
```
**Search functional in isolation (40k rows):**
```
FULL bm25 SQL OK rows= 50            # query "shirt", one merchant
has_tsv=1000  has_embedding=1000     # all rows indexed + embedded
```
**Search degradation under concurrent sync (the contention finding):**
```
ILIKE fallback also failed: InFailedSQLTransactionError:
  current transaction is aborted, commands ignored until end of transaction block
bm25_search len= 0
```
**Webhook 500 root cause (app log):**
```
asyncpg.exceptions.NotNullViolationError: null value in column "in_stock"
  of relation "product_cache" violates not-null constraint
  at src/app/modules/webhooks/service.py:269 upsert_product
```
**Agent samples (real Aria output over text WS):**
```
off-topic  "what's the weather today?"  -> "I'm here to help you shop at this store. What can I find for you?"
halluc.    "purple dragon costume XXL?" -> "Tell me the product name and size, and I'll check live stock."
hindi      "mujhe kuch sasta dikhao"    -> "बिलकुल, ब्लू कॉटन टी-शर्ट एक अच्छा सस्ता ऑप्शन ..."
beta       "show me earbuds"            -> "The Wireless Earbuds Pro are noise cancelling ..."
```

## 12. Appendix C — Artifact inventory

| File | Purpose |
|---|---|
| `TESTING_OVERVIEW.md` | This document — complete testing record |
| `TEST_REPORT.md` | Functional campaign summary (Part A) |
| `test_store/test_store.py` | Small 3-variant store (functional campaign) |
| `test_store/test_store_scale.py` | Per-merchant 1000-product generator (scale) |
| `test_store/run_test_campaign.py` | Functional campaign harness |
| `test_store/run_scale_test.py` | Scale harness |

To run a harness inside the container, copy it into a mounted dir (e.g.
`backend/static/`) and `docker exec docker-app-1 python static/<harness>.py`.
