# Agentic Commerce

Multi-tenant SaaS backend supporting WordPress, Shopify, and custom
e-commerce integrations behind a single API.

## Project layout

  - `app/` — clean-architecture backend (under construction)
  - `wooagent-backend/` — legacy single-tenant backend (migrated module
    by module)
  - `wooagent/` — legacy WordPress plugin (moves to
    `frontends/wordpress-plugin/` in Module 7)
  - `frontends/` — Shopify embedded app, WordPress plugin, hosted
    dashboard (placeholders)
  - `tests/` — unit, integration, e2e
  - `ops/` — Docker, Compose, K8s
  - `docs/` — ADRs, runbooks, API docs

## Quick start

### 1. Get your Supabase connection strings

Go to your Supabase project: Project Settings -> Database -> Connection
string. Copy BOTH connection URLs:

  - **Pooled (Transaction mode)**: port 6543. Used by the app.
  - **Direct connection**: port 5432. Used by migrations.

### 2. Create the .env file

```bash
cp .env.example .env
```

Edit `.env` and replace the placeholders with your real Supabase URLs.

### 3. Start the dev stack

```bash
docker compose -f ops/compose/docker-compose.dev.yml up -d
```

This brings up:
  - Redis on localhost:6379
  - FastAPI app on localhost:8000

(Postgres is hosted on Supabase, not local.)

### 4. Run migrations

From the host machine (not the container, because alembic needs the
direct URL):

```bash
pip install -e ".[dev,test]"
alembic upgrade head
```

### 5. Verify

```bash
curl http://localhost:8000/health
curl http://localhost:8000/ready
curl http://localhost:8000/version
```

Open http://localhost:8000/docs in a browser.

### 6. Stop the stack

```bash
docker compose -f ops/compose/docker-compose.dev.yml down
```

## Local development without Docker

```bash
pip install -e ".[dev,test]"

# Bring up Redis only
docker compose -f ops/compose/docker-compose.dev.yml up -d redis

# Override REDIS_URL to localhost
export REDIS_URL=redis://localhost:6379/0

# Run migrations
alembic upgrade head

# Run the app
uvicorn app.main:create_app --factory --reload
```

## Running tests

```bash
pytest -m unit                 # fast, no DB
pytest -m integration          # requires Supabase + Redis
pytest                         # everything
```

## Architecture

Clean architecture with four rings:

  1. `app/domain/` — pure business types
  2. `app/application/` — use cases and port interfaces
  3. `app/infrastructure/` — concrete implementations
  4. `app/interfaces/` — HTTP, WebSocket, CLI, workers

See `ARCHITECTURE.md` for details.

## Status

Day 1 — Module 1 thin foundation complete. Next: Day 2 — tenant core.
