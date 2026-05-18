from fastapi import Depends
from ..auth.dependencies import get_current_user


async def require_user(payload: dict = Depends(get_current_user)) -> dict:
    return payload
