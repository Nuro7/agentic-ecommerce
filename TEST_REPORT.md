# Speako — QA Test Campaign Report

Automated black-box + in-process testing across **3 simulated merchants** plus a
**sampled set of real Aria conversations**. Driven by `test_store/run_test_campaign.py`
against the live Docker stack.

- **Result:** 32 PASS · 1 WARN · 2 FAIL (real bugs) · 0 crashes in the harness
- **Merchants:** Alpha (apparel, ids 101-105), Beta (electronics, 201-205), Gamma (home, 301-304),
  each a `custom_api` tenant backed by its own test store.
- **Agent turns:** 9 real LLM/voice turns (within the 12 cap). Aria responded for real
  (Grok/GPT/Gemini + Google TTS), including Hindi.
- **Not covered (tooling limits):** real microphone/STT audio quality, audio playback/barge-in,
  and visual widget rendering.

---

## 🔴 Confirmed bugs (fix before onboarding real merchants)

### 1. Custom webhook crashes (500) when a product omits `in_stock` — MEDIUM-HIGH
`POST /api/v1/webhooks/custom/{tenant}` with a **valid HMAC** and a product payload that
omits `in_stock` returns **500**:
```
asyncpg.exceptions.NotNullViolationError: null value in column "in_stock"
  of relation "product_cache" violates not-null constraint
  at src/app/modules/webhooks/service.py:269  upsert_product
```
The full **sync** path normalizes products through the canonical adapter (which defaults
`in_stock=True`, price, etc.), but the **webhook `upsert_product`** path inserts raw fields
and hits the NOT-NULL constraint. Any real custom platform that sends partial product
updates (very common) will get 500s and silently fail to sync that product.
**Fix:** route webhook upserts through the same adapter/normalizer as sync, or default the
NOT-NULL columns (`in_stock`, `price`, `stock_quantity`) in `upsert_product`.

### 2. `test-connection` reports success for an unreachable store — MEDIUM
`POST /api/v1/onboard/test-connection` against a dead URL (nothing listening) returns:
```
{"ok": true, "products_found": 0, "message": "Connection successful. Found 0 product(s)."}
```
Because `CustomApiClient.search_products` catches all exceptions and returns `[]`, the
test-connection handler never sees a failure — so it can **essentially never return
`ok:false`** for `custom_api`. A merchant who typos their API URL is told "Connection
successful," then nothing works. **Fix:** in `test_store_connection`, treat a connection
error (or `products_found == 0` with an explicit reachability probe like `GET /store/info`
or `/health`) as `ok:false`, or have the client surface a distinct "unreachable" signal.

---

## 🟠 Quality / design findings (confirm intent)

### 3. Voice gate blocks the assistant entirely for Starter (free) merchants — HIGH (confirm)
Every `/wooagent/stream` WebSocket connection is unconditionally treated as voice
(`enforce_conversation_quota(..., is_voice=True)` + the `allow_voice` plan flag,
`billing/dependencies.py:108`). Starter has `allow_voice:false`, so a Starter merchant's
widget gets:
```
pipeline_error: Voice is not available on your current plan. Upgrade to Growth or Pro.
```
The widget's only conversation path is this WS, so **Starter customers can't chat with Aria
at all — not even text.** If Starter is meant to have text chat, the WS must allow a
text-only pipeline (Pipeline C) without the voice gate. If that's intended (voice/text both
paid), ignore — but it's surprising for an entry tier.

### 4. "Show me your products" surfaces one OUT-OF-STOCK item — MEDIUM (agent quality)
Asked to show products, Alpha's Aria replied:
> "We have this **Leather Wallet** that's pretty handy… It's **out of stock** at the moment."
It led with the single out-of-stock SKU (id 104, qty 0) instead of listing in-stock items.
Poor merchandising. **Fix:** bias product presentation toward in-stock items and return
several, not one, for a broad "show me your products" query.

### 5. Add-to-cart emits `show_cart` action, not an explicit add — WARN (verify)
"Add the blue cotton t-shirt" → Aria said *"added that blue cotton t-shirt to your cart!"*
and emitted a `show_cart` UI action (no `add_to_cart` action). The verbal confirmation is
right; cart-content persistence wasn't independently verified in this run. Worth a targeted
check that the item actually lands in the cart and that the UI action matches the operation.

---

## ✅ What works well (verified)

| Area | Result |
|---|---|
| Onboarding (3 merchants, custom_api) | PASS — 201 + tenant_id each |
| Negative onboarding | PASS — duplicate→409, missing url→422, bad platform→422, malformed JSON→422 |
| Product sync per merchant | PASS — alpha=5, beta=5, gamma=4 upserted, exact catalog match |
| **Cross-tenant product isolation** | PASS — alpha∩beta platform_ids = ∅ |
| **Cart routing isolation** | PASS — item in alpha's cart, beta's cart empty for same session |
| **IDOR probe** | PASS — alpha's JWT on beta's `/tenants/{id}` → 403 |
| Auth login + `/tenants/me` | PASS |
| **Quota enforcement** | PASS — over plan limit → 402 |
| Rate limiting | PASS — burst `/cart` → 429s |
| Webhook signature | PASS — bad HMAC→401, missing HMAC→401 |
| Voice WS missing/short `session_id` | PASS — rejected (validates the earlier anonymous-session fix) |
| 5 concurrent greets, same session | PASS — no 5xx (validates the session-lock fix) |
| **Agent: off-topic guard** | PASS — "what's the weather?" → "I'm here to help you shop." |
| **Agent: hallucination guard** | PASS — fake "purple dragon costume XXL" → no fabrication, asks to check live stock |
| **Agent: multilingual** | PASS — Hindi query answered in Hindi (Devanagari) |
| **Agent: per-merchant catalog** | PASS — beta returns electronics ("Wireless Earbuds Pro"), alpha returns apparel; no cross-leak |

---

## How to reproduce
1. Start 3 test stores on the host (ports 9001/9002/9003, variants alpha/beta/gamma).
2. `docker exec -e RUN_ID=<unique> docker-app-1 python static/run_test_campaign.py`
   (the harness lives in `test_store/run_test_campaign.py`; copy into a container-mounted
   dir such as `backend/static/` or `backend/src/` to run via `docker exec`).
3. Findings 1–2 are deterministic; agent findings (3–5) depend on live LLM output.

## Notes / limits
- Agent conversations cost real LLM + TTS credits; this run used 9 turns by design.
- Voice audio quality, STT accuracy, and the rendered widget UI were **not** testable here
  (no microphone/browser); everything beneath them was exercised.
