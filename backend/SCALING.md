# Speako — Scaling & Multi-Tenant Isolation Notes

## Postgres Row-Level Security (RLS) — the DB-level tenant backstop

RLS is enabled (migration `0013_enable_rls.py`) on the **customer-conversation** tables so that even a
query missing `WHERE tenant_id = …` returns only the current tenant's rows.

**RLS-protected tables:** `product_cache`, `cart_items`, `conversations`, `orders`, and `messages`
(scoped through its parent `conversations` via an `EXISTS` policy — it has no `tenant_id` column).

**Deliberately EXCLUDED** (keep their app-level `WHERE tenant_id` + JWT protection):
- `tenants`, `refresh_tokens`, `users` — looked up **before** a tenant is known (resolver, token hash,
  user id), so RLS would break auth.
- `subscriptions`, `usage_records`, `webhook_events`, `conversation_metrics`, `plans` — worker-written
  and JWT-read; the workers legitimately scan them **cross-tenant** (`webhooks.process_pending`,
  billing invoice-all), which RLS would zero out.

### How the tenant GUC is set
The policies filter on `current_setting('app.tenant_id', true)`. That GUC is set **transaction-locally**
(`set_config(..., true)`) so it auto-clears on commit/rollback and never leaks across pooled connections.

- **Request / WebSocket / agent paths** — auto-scoped. `set_request_tenant(tenant_id)`
  (`core/database.py`) stores the tenant in a `ContextVar` at the tenant boundary
  (`get_authenticated_tenant`, `get_tenant_store_client`, `resolve_tenant_id_from_request`, the WS
  handler, and defensively `ask_brain`). A global `after_begin` event on the SQLAlchemy `Session` class
  applies the GUC on **every** new transaction (incl. after a mid-request commit). The event is a no-op
  on non-Postgres dialects, so the SQLite test suite is unaffected.
- **Celery workers** — set the GUC **explicitly per tenant/event** with `set_tenant_guc(session,
  tenant_id)`, because a worker's single transaction spans many tenants (the once-per-transaction event
  is not enough). Already wired in `sync_products._sync_async` / `_diff_sync_async` (per-tenant loop)
  and `webhooks.service.process_pending` (per event).

### ⚠️ Request-session timing — the `after_begin` event is NOT enough by itself
The `after_begin` event reads the ContextVar **at transaction-begin**. But the tenant-resolution
dependencies query `tenants` first (which *begins* the request's transaction at GUC='') and only then
set the tenant — and the endpoint **reuses that same session** (FastAPI caches `Depends(get_db)`). So
the GUC would stay '' for the rest of the request → RLS reads come back empty and RLS writes fail
`WITH CHECK`. **Rule:** after resolving a tenant, **also call `await set_tenant_guc(db, tenant.id)` on
the request session** (not only `set_request_tenant`). This is wired in `get_authenticated_tenant`,
`get_tenant_store_client`, `resolve_tenant_id_from_request`, and the inline-auth `POST /ingest`
endpoint. `set_request_tenant` (ContextVar) still covers **fresh** sessions opened later in the request
(e.g. `hybrid_search`) and post-commit re-begins. **Any new endpoint that resolves a tenant inline (not
via those deps) and touches an RLS table must call `set_tenant_guc(db, tenant_id)` itself.**

### Single DB role → no BYPASSRLS
All services (web, worker, beat) share one `DATABASE_URL` / DB role (`agentic`), which **owns** the
tables — hence `FORCE ROW LEVEL SECURITY` (RLS applies to the owner too). `BYPASSRLS` is **not** an
option: granting it to the shared role would disable RLS for the web app as well.

### ⚠️ The app DB role MUST NOT be a SUPERUSER (or have BYPASSRLS)
**Postgres superusers — and any role with `BYPASSRLS` — bypass RLS entirely, even with `FORCE`.** If
the app connects as a superuser, RLS silently does nothing and every WHERE-less query leaks across
tenants. Verify the production role:
```sql
SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user;  -- must be (f, f)
```
The local `docker-compose.dev.yml` Postgres creates `agentic` as a **superuser** (the postgres image
default), so RLS is bypassed there. To exercise RLS locally, connect as a non-superuser:
```sql
CREATE ROLE rls_app LOGIN PASSWORD '…' NOSUPERUSER NOBYPASSRLS;
GRANT USAGE ON SCHEMA public TO rls_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO rls_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO rls_app;
```
On Render/managed Postgres the default app user is typically **not** a superuser, so RLS applies — but
confirm with the query above before relying on it.

### Automatic boot-time enforcement (you no longer have to remember the query)
`server.py` `_assert_rls_role_safe()` runs that check at **startup**: on Postgres, if RLS is enabled on
`product_cache` (migration 0013 applied) **and** the connected role has `rolsuper` or `rolbypassrls`,
the app **refuses to boot** (`RuntimeError`). This front-runs the staging soak — a superuser role is
caught at boot, before any soak. It's gated on RLS actually being enabled, so local dev (superuser
`agentic`, 0013 not applied) still boots with a warning. Override for the rare legitimate case with
`SPEAKO_ALLOW_RLS_BYPASS=true` (logs a warning instead of refusing).

## Rules for future changes (default-on for new tables)
Postgres does **not** auto-apply RLS to new tables. So:
1. **New tenant-scoped customer table** → add this block in its migration:
   ```sql
   ALTER TABLE <t> ENABLE ROW LEVEL SECURITY;
   ALTER TABLE <t> FORCE ROW LEVEL SECURITY;
   CREATE POLICY tenant_isolation ON <t>
     USING (tenant_id = current_setting('app.tenant_id', true))
     WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
   ```
   (Child table without `tenant_id`? Use an `EXISTS` policy over its parent, like `messages`.)
2. **New worker that writes an RLS table** → call `await set_tenant_guc(db, tenant_id)` per tenant
   before the writes, in the same transaction.
3. **New cross-tenant worker scan** → keep that table RLS-excluded (or it returns zero rows).

### Rollback
`alembic downgrade -1` drops the policies and disables RLS instantly, with no data loss — the rollback
valve if any path unexpectedly returns zero rows.

### Dev note
With RLS migrated on a **dev** Postgres, the single-tenant `app.state.store_client` fallback (no resolved
tenant) leaves the GUC empty → product queries return nothing. In dev, resolve a real tenant
(`?shop=` / `X-Tenant-ID`) or don't apply `0013` locally.

---

## Scaling out (multi-replica)

**The app targets a SINGLE web process today** (`render.yaml` `speako-web`: no `numInstances`/autoscaling
→ 1 instance; Dockerfile `CMD` runs `uvicorn` with no `--workers` → 1 process). A startup **scale-guard**
(`server.py` `_assert_single_process_or_acked`) enforces this: if `WEB_CONCURRENCY > 1` or
`SPEAKO_WEB_REPLICAS > 1`, the app **refuses to boot** unless `SPEAKO_ALLOW_MULTI_PROCESS=true` is set —
so the assumption can't be violated silently.

Before scaling to 2+ web processes, make this per-process state Redis-shared:

| Concern | Where | Why it breaks at 2+ processes | Fix |
|---------|-------|-------------------------------|-----|
| **Voice concurrency cap** | `api/v1/voice.py` `_voice_active` | Each process counts its own sessions → effective cap is `N × VOICE_MAX_CONCURRENT` against the **global** Gemini Live quota | Redis counter — mirror `core/ratelimit.py` (`redis.incr` + `expire(..., nx=True)`); decrement in the existing `finally`; crash-safety via a short TTL heartbeat or accept minor drift |
| **LLM circuit breakers** | `core/circuit_breaker.py` (instances in `agent/llm_router.py`, `agent/voice/pipelines/router.py`) | Breaker state (`_failures`/`_state`/`_last_fail_at`) is unshared → one process trips while another keeps hammering a dead provider | Share state in Redis. **Caveat:** this adds a Redis round-trip to the LLM hot path — consider a short local cache of the breaker state |
| **Store-client cache** | `integrations/factory.py` `_CLIENT_CACHE` | Not a correctness problem — it's a per-process perf cache, now **bounded** by `CLIENT_CACHE_MAX` (oldest-first eviction) | No change needed at scale |

### Env knobs
- `CLIENT_CACHE_MAX` (default `500`) — max cached store clients per process; oldest evicted past this.
- `WEB_CONCURRENCY` / `SPEAKO_WEB_REPLICAS` — read by the scale-guard to detect multi-process.
- `SPEAKO_ALLOW_MULTI_PROCESS=true` — operator acknowledgement that overrides the guard (logs a warning
  instead of refusing to boot). Only set this once the voice cap + breakers are Redis-shared.

### P3 backlog (reliability — not scale-out)
Health checks (sync-stale, embeddings-down), promote silent excepts to warnings, rolling history
summary, multi-step tool-round cap, `compare_products` parallelize. See plan file.
