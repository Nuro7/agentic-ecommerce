from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
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


async def init_db() -> None:
    # Tables are created via Alembic migrations, not create_all.
    # Run: alembic upgrade head
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
