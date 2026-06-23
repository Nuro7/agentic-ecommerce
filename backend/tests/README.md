# Speako backend test suite

## Layout
| File | Covers |
|------|--------|
| `test_security.py` | Pre-existing Tier 1-2 security: JWT-protected routes, login, cross-tenant 403, Shopify webhook signature, rate-limiter TTL repair. |
| `test_tenant_isolation.py` | **P0** multi-tenant isolation: no-tenant reject in prod (P0-1), session/state/cart unreadable across tenants + WS-token binding (P0-2), facts isolation (P0-3), catalog-cache scoping (P0-5), MVP_MODE prod gate (P0-6). |
| `test_guardrails.py` | **P1** anti-hallucination: digit-free / numbered-variant / symbol-less fakes + real reorder/partial pass (P1-8/8b/8c), never-skipped grounding (P1-9), voice transcript monitor (P1-11). Pure unit — no DB/Redis. |
| `test_rls.py` | **P0-4 Postgres RLS** — `@pytest.mark.integration`, Postgres-gated (see below). |
| `test_startup_guards.py` | **P2** boot guards in `server.py`: multi-process scale-guard (P2-11/12) + RLS-role guard (refuse to boot if the DB role bypasses RLS). |
| `test_reliability.py` | **P2/P3** hardening: logged silent-excepts (P3-15), `/ops` signals (P3-14), concurrent compare_products (P3-18a), bounded client cache (P2-13), stock-less webhook upsert (`in_stock` fix / migration 0014). |

## Running

```bash
# Default: SQLite in-memory, no external services. RLS integration tests SKIP.
pytest tests/ -q          # → 62 passed, 2 skipped

# Postgres RLS integration tests — require a MIGRATED Postgres + a NON-SUPERUSER role
# (superusers/BYPASSRLS bypass RLS, so RLS must be exercised as a plain role):
docker run -d --name pg -e POSTGRES_DB=agentic_commerce -e POSTGRES_USER=agentic \
  -e POSTGRES_PASSWORD=agentic -p 5433:5432 pgvector/pgvector:pg16
DATABASE_URL=postgresql+asyncpg://agentic:agentic@localhost:5433/agentic_commerce \
  alembic upgrade head
docker exec pg psql -U agentic -d agentic_commerce -c \
  "CREATE ROLE rls_test LOGIN PASSWORD 'rls_test' NOSUPERUSER NOBYPASSRLS; \
   GRANT USAGE ON SCHEMA public TO rls_test; \
   GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO rls_test;"
RLS_TEST_DATABASE_URL=postgresql+asyncpg://rls_test:rls_test@localhost:5433/agentic_commerce \
  pytest -m integration -v   # → 2 passed
```

## The gated / SQLite split — and why search-isolation lives in the Postgres tests
The default suite runs on in-memory SQLite (fast, no services). Two things genuinely **cannot** be
proven on SQLite, so they're gated behind `RLS_TEST_DATABASE_URL`:

1. **Row-Level Security** — SQLite has no `set_config`/policies. The `after_begin` GUC event and
   `set_tenant_guc` are guarded to PostgreSQL only, so the SQLite suite stays green while RLS is proven
   under real Postgres.
2. **Cross-tenant SEARCH isolation** — the agent's product search (`hybrid_search`) reads
   `product_cache` via Postgres `tsvector`/`pgvector`, which SQLite doesn't have. The **search table is
   `product_cache`**, and `test_rls.py::test_rls_hides_other_tenant_and_shows_own` proves a tenant-scoped
   connection sees **only its own** `product_cache` rows on a `WHERE`-less select. RLS therefore makes
   search structurally tenant-isolated at the row layer — that test *is* the search-isolation proof.
   (A full end-to-end `hybrid_search` test would additionally need Redis + an embeddings provider, so the
   row-level RLS proof is the gated, dependency-light equivalent.)

Gating is on the explicit `RLS_TEST_DATABASE_URL` env var (not `settings.database_url`, whose default is
already a `postgresql://…` string) so `pytest tests/` skips cleanly on a machine with no Postgres.
