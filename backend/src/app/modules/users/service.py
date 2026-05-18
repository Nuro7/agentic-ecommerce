from sqlalchemy.ext.asyncio import AsyncSession
from .repository import UserRepository
from .models import User
from .schemas import UserCreate
from ...core.security import hash_password
from ...core.exceptions import ConflictError, NotFoundError


class UserService:
    def __init__(self, db: AsyncSession):
        self.repo = UserRepository(db)

    async def create_user(self, tenant_id: str, data: UserCreate) -> User:
        existing = await self.repo.get_by_email(data.email, tenant_id)
        if existing:
            raise ConflictError()
        user = User(
            tenant_id=tenant_id,
            email=data.email,
            name=data.name,
            role=data.role,
            password_hash=hash_password(data.password),
        )
        return await self.repo.create(user)

    async def get_user(self, user_id: str) -> User:
        user = await self.repo.get_by_id(user_id)
        if not user:
            raise NotFoundError()
        return user

    async def list_users(self, tenant_id: str) -> list[User]:
        return await self.repo.list_by_tenant(tenant_id)
