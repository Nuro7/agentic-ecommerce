from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, update
from .models import RefreshToken, MagicToken
from datetime import datetime, timezone


class RefreshTokenRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, token: RefreshToken) -> RefreshToken:
        self.db.add(token)
        await self.db.commit()
        await self.db.refresh(token)
        return token

    async def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        result = await self.db.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        return result.scalar_one_or_none()

    async def revoke(self, token_hash: str) -> None:
        await self.db.execute(delete(RefreshToken).where(RefreshToken.token_hash == token_hash))
        await self.db.commit()

    async def revoke_all_for_tenant(self, tenant_id: str) -> None:
        await self.db.execute(delete(RefreshToken).where(RefreshToken.tenant_id == tenant_id))
        await self.db.commit()


class MagicTokenRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, token: MagicToken) -> MagicToken:
        self.db.add(token)
        await self.db.commit()
        await self.db.refresh(token)
        return token

    async def get_by_hash(self, token_hash: str) -> MagicToken | None:
        result = await self.db.execute(
            select(MagicToken).where(MagicToken.token_hash == token_hash)
        )
        return result.scalar_one_or_none()

    async def mark_used(self, magic_id: str, now: datetime) -> None:
        await self.db.execute(
            update(MagicToken).where(MagicToken.id == magic_id).values(used_at=now)
        )
