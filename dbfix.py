"""One-off DB maintenance: clear stuck sessions + report locks + cache count.

Run inside the speako-worker image so it reuses DATABASE_URL from .env.worker
(no need to paste the connection string):

    docker run --rm --env-file backend/.env.worker -v "${PWD}/dbfix.py:/dbfix.py" speako-worker python /dbfix.py
"""
import asyncio
import os

import asyncpg

TENANT = "252d2f28-368f-4035-9077-2965ab8cab32"

# .env.worker holds the SQLAlchemy form (postgresql+asyncpg://...?ssl=require).
# asyncpg wants a plain DSN + an explicit ssl arg.
_url = os.environ["DATABASE_URL"]
_dsn = _url.replace("postgresql+asyncpg://", "postgresql://").split("?")[0]


async def main() -> None:
    conn = await asyncpg.connect(_dsn, ssl="require")
    try:
        killed = await conn.fetch(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid() "
            "AND state = 'idle in transaction'"
        )
        print(f"[1] terminated idle-in-transaction sessions: {len(killed)}")

        rows = await conn.fetch(
            "SELECT pid, state, wait_event_type, wait_event, "
            "pg_blocking_pids(pid) AS blocked_by, left(query, 90) AS q "
            "FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()"
        )
        print(f"[2] sessions: {len(rows)}")
        for r in rows:
            print("    ", dict(r))

        cnt = await conn.fetchrow(
            "SELECT count(*) AS n FROM product_cache WHERE tenant_id = $1",
            TENANT,
        )
        print(f"[3] product_cache rows for cartify: count={cnt['n']}")
        cols = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'product_cache' ORDER BY ordinal_position"
        )
        print("[4] product_cache columns:", [c["column_name"] for c in cols])
    finally:
        await conn.close()


asyncio.run(main())
