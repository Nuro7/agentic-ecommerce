"""Postgres Row-Level Security integration test (P0-4).

Gated on RLS_TEST_DATABASE_URL pointing at a MIGRATED Postgres (alembic upgrade head),
so the default SQLite suite skips it cleanly:

    RLS_TEST_DATABASE_URL=postgresql+asyncpg://agentic:agentic@localhost:5432/agentic_commerce \
        pytest tests/test_rls.py -v
"""
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("RLS_TEST_DATABASE_URL"),
        reason="set RLS_TEST_DATABASE_URL to a migrated Postgres to run RLS tests",
    ),
]


async def _set_guc(conn, tenant_id: str) -> None:
    await conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})


async def test_rls_hides_other_tenant_and_shows_own():
    eng = create_async_engine(os.environ["RLS_TEST_DATABASE_URL"], poolclass=NullPool)
    a = "rls-A-" + uuid.uuid4().hex[:8]
    b = "rls-B-" + uuid.uuid4().hex[:8]
    try:
        async with eng.begin() as conn:
            # tenants is RLS-excluded → insert without a GUC.
            for tid in (a, b):
                await conn.execute(
                    text("INSERT INTO tenants (id, name, email) VALUES (:id, :n, :e)"),
                    {"id": tid, "n": tid, "e": f"{tid}@example.com"},
                )
            # product_cache is RLS+FORCE with WITH CHECK → the GUC must equal the row's
            # tenant_id at insert time (this also exercises WITH CHECK).
            await _set_guc(conn, a)
            await conn.execute(
                text("INSERT INTO product_cache (id, tenant_id, platform_id, name) "
                     "VALUES (:id, :t, :p, :n)"),
                {"id": uuid.uuid4().hex, "t": a, "p": "pA", "n": "Watch A"},
            )
            await _set_guc(conn, b)
            await conn.execute(
                text("INSERT INTO product_cache (id, tenant_id, platform_id, name) "
                     "VALUES (:id, :t, :p, :n)"),
                {"id": uuid.uuid4().hex, "t": b, "p": "pB", "n": "Watch B"},
            )

            # Scope to A and run a WHERE-LESS select — RLS must filter it.
            await _set_guc(conn, a)
            rows = (await conn.execute(text("SELECT tenant_id FROM product_cache"))).scalars().all()

            # #6b positive: A sees its OWN rows (guards against a GUC-scope bug that hides all).
            assert any(r == a for r in rows), "tenant A should see its own product_cache rows"
            # #6a hide: B's rows are invisible despite no WHERE clause.
            assert all(r == a for r in rows), f"RLS leak — saw non-A rows: {set(rows)}"
            assert b not in rows
    finally:
        async with eng.begin() as conn:
            for tid in (a, b):
                await _set_guc(conn, tid)
                await conn.execute(text("DELETE FROM product_cache WHERE tenant_id = :t"), {"t": tid})
            await conn.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": a, "b": b})
        await eng.dispose()


async def test_rls_same_session_scoped_after_begin_at_empty():
    """Regression for the GUC-timing gap (P0-4b): the authenticated REST endpoints reuse
    the request session whose transaction BEGAN at GUC='' (during the tenants lookup).
    Proves that scoping that SAME session mid-transaction (what the resolution deps now
    do via set_tenant_guc) makes RLS reads return rows and writes pass WITH CHECK."""
    from src.app.core.database import set_request_tenant, set_tenant_guc

    eng = create_async_engine(os.environ["RLS_TEST_DATABASE_URL"], poolclass=NullPool)
    Sess = async_sessionmaker(eng, expire_on_commit=False)
    a = "rls-S-" + uuid.uuid4().hex[:8]
    try:
        # Seed tenant A + one A product (raw conn with explicit GUC).
        async with eng.begin() as conn:
            await conn.execute(text("INSERT INTO tenants (id, name, email) VALUES (:i,:n,:e)"),
                               {"i": a, "n": a, "e": f"{a}@example.com"})
            await _set_guc(conn, a)
            await conn.execute(text("INSERT INTO product_cache (id,tenant_id,platform_id,name) "
                                    "VALUES (:i,:t,:p,:n)"),
                               {"i": uuid.uuid4().hex, "t": a, "p": "pA", "n": "Seed A"})

        # Simulate a request: context empty, ORM Session (so the real after_begin fires).
        set_request_tenant("")
        async with Sess() as s:
            # First query hits an EXCLUDED table → begins the txn → after_begin sets GUC=''.
            await s.execute(text("SELECT count(*) FROM tenants"))
            # Precondition (the bug): at GUC='', an RLS read on the same session is empty.
            assert (await s.execute(text("SELECT count(*) FROM product_cache"))).scalar() == 0

            # The fix: scope this SAME session (what the resolution deps now do).
            await set_tenant_guc(s, a)
            rows = (await s.execute(text("SELECT tenant_id FROM product_cache"))).scalars().all()
            assert rows and all(r == a for r in rows)              # read returns own rows
            await s.execute(text("INSERT INTO product_cache (id,tenant_id,platform_id,name) "
                                 "VALUES (:i,:t,:p,:n)"),
                            {"i": uuid.uuid4().hex, "t": a, "p": "pW", "n": "Write A"})  # WITH CHECK passes
            await s.commit()
    finally:
        async with eng.begin() as conn:
            await _set_guc(conn, a)
            await conn.execute(text("DELETE FROM product_cache WHERE tenant_id = :t"), {"t": a})
            await conn.execute(text("DELETE FROM tenants WHERE id = :i"), {"i": a})
        await eng.dispose()
        set_request_tenant("")
