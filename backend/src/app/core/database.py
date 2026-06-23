from contextlib import asynccontextmanager
from contextvars import ContextVar

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session
from sqlalchemy.pool import NullPool
from ..config import settings

# ── Connection pool sizing ────────────────────────────────────────────────────
# Target: 100 active merchants, each with concurrent customer sessions.
#
# pool_size=20   — base connections kept open at all times.
# max_overflow=40 — extra connections allowed under burst load (total max = 60).
# pool_timeout=30 — wait up to 30 s for a connection before raising; prevents
#                   silent queue build-up under heavy load.
# pool_recycle=1800 — recycle connections every 30 min to avoid stale TCP
#                     connections dropped by the DB or load balancer.
# pool_pre_ping=True — verify connection health before each checkout; avoids
#                      "server closed the connection unexpectedly" errors after
#                      idle periods.
#
# At 60 max connections, each PostgreSQL connection uses ~5 MB RAM.
# Total overhead: ~300 MB on the DB server — well within standard limits.
# Bump pool_size if p99 latency rises under load.
engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
    pool_timeout=30,
    pool_recycle=1800,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# ── Worker DB session (Celery) ────────────────────────────────────────────────
# Each Celery task runs in its OWN asyncio.run() event loop. A module-level
# engine (even NullPool) can leave asyncpg connection objects bound to a previous,
# now-closed loop → "got Future attached to a different loop" / "Event loop is
# closed". The bulletproof fix: build a fresh NullPool engine bound to the CURRENT
# loop and dispose it before that loop exits, so nothing ever survives a task.
@asynccontextmanager
async def worker_session():
    eng = create_async_engine(settings.database_url, poolclass=NullPool)
    factory = async_sessionmaker(eng, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await eng.dispose()


class Base(DeclarativeBase):
    pass


# ── Row-Level Security: per-request tenant GUC ───────────────────────────────
# RLS policies on the customer-data tables filter by current_setting('app.tenant_id').
# We set that GUC transaction-locally (is_local=true) so it's auto-cleared on
# commit/rollback and never leaks across pooled connections. The request path sets
# a ContextVar at the tenant boundary (dependencies / WS handler / ask_brain); the
# after_begin event below applies it on EVERY new transaction (incl. after a
# mid-request commit). Workers, which span many tenants in one transaction, instead
# call set_tenant_guc() explicitly per tenant (see workers/tasks/*).
current_tenant_id: ContextVar[str] = ContextVar("current_tenant_id", default="")


def set_request_tenant(tenant_id: str | None) -> None:
    """Bind the current async context to a tenant for RLS (request/WS paths)."""
    current_tenant_id.set(tenant_id or "")


@event.listens_for(Session, "after_begin")
def _apply_tenant_guc(session, transaction, connection):
    # SQLite (the test harness) has no set_config — guard so the in-memory suite
    # keeps working. Only Postgres enforces RLS.
    if connection.dialect.name != "postgresql":
        return
    connection.execute(
        text("SELECT set_config('app.tenant_id', :t, true)"),
        {"t": current_tenant_id.get()},
    )


async def set_tenant_guc(session: AsyncSession, tenant_id: str) -> None:
    """Explicitly scope a session to a tenant — for the request session after tenant
    resolution (its transaction already began at GUC='') and for worker tasks whose
    single transaction spans many tenants (the after_begin event fires only once)."""
    bind = session.bind
    if bind is not None and bind.dialect.name != "postgresql":
        return  # SQLite (tests) has no set_config — no-op, like the after_begin event.
    await session.execute(
        text("SELECT set_config('app.tenant_id', :t, true)"),
        {"t": tenant_id or ""},
    )


async def init_db() -> None:
    # Tables are created via Alembic migrations, not create_all.
    # Run: alembic upgrade head
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            # Defensive: clear the tenant ContextVar at request teardown so a later
            # request reusing this context can't inherit a previous tenant's RLS scope.
            # (The GUC itself is txn-local, so this is belt-and-suspenders.)
            set_request_tenant("")
