from .database import init_db
from .cache import init_cache
from .logging import configure_logging


async def on_startup() -> None:
    configure_logging()
    await init_db()
    await init_cache()


async def on_shutdown() -> None:
    pass
