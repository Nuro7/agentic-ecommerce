from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from ..config import settings

engine = create_async_engine(settings.database_url, echo=settings.debug)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def init_db() -> None:
    # Tables are created via Alembic migrations, not create_all.
    # Run: alembic upgrade head
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
