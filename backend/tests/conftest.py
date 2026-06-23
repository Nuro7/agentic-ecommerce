"""Shared pytest fixtures."""
import asyncio
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from src.app.core.database import Base, get_db

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
async def engine():
    # Import the app so every model module registers its table on Base.metadata
    # BEFORE create_all runs — otherwise tables only referenced at runtime (e.g.
    # refresh_tokens) are missing. (create_app is imported lazily elsewhere to keep
    # pure-unit tests free of the full app tree; the DB fixtures genuinely need it.)
    from src.app.server import create_app  # noqa: F401  (import side effect: model registration)
    create_app()
    eng = create_async_engine(TEST_DATABASE_URL)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db(engine):
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session


@pytest.fixture
async def client(db):
    # Imported lazily so unit tests that don't need the full ASGI app (and its
    # heavy transitive deps) can still collect/run when the app tree isn't installed.
    from src.app.server import create_app
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
