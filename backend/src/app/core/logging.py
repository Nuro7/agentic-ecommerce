import logging
import sys
from ..config import settings


def configure_logging() -> None:
    log_level = settings.log_level.upper()
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        stream=sys.stdout,
        force=True,
    )
    logging.getLogger().setLevel(log_level)
    # Ensure src and websockets loggers are set to INFO
    logging.getLogger("src").setLevel(logging.INFO)
    logging.getLogger("websockets").setLevel(logging.INFO)
