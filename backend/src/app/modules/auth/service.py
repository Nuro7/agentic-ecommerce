import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from .repository import RefreshTokenRepository
from .models import RefreshToken
from .schemas import LoginRequest, TokenResponse
from ..tenants.repository import TenantRepository
from ...core.security import verify_password, hash_password, create_access_token
from ...core.exceptions import UnauthorizedError

# Pre-computed once at import so login does equal argon2 work whether or not the
# account exists — prevents user-enumeration via response timing.
_DUMMY_HASH = hash_password("speako-invalid-account")


class AuthService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.token_repo = RefreshTokenRepository(db)
        self.tenant_repo = TenantRepository(db)

    async def login(self, data: LoginRequest) -> TokenResponse:
        tenant = await self.tenant_repo.get_by_email(data.email)
        # Fail closed and run a verify even when the tenant/hash is missing, so a
        # missing account is indistinguishable (timing) from a wrong password.
        stored_hash = getattr(tenant, "hashed_password", None) if tenant else None
        if not verify_password(data.password, stored_hash or _DUMMY_HASH):
            raise UnauthorizedError()
        if not tenant or not tenant.is_active or not stored_hash:
            raise UnauthorizedError()
        access_token = create_access_token({"sub": tenant.id, "email": tenant.email})
        raw_refresh = secrets.token_urlsafe(48)
        token_hash = hashlib.sha256(raw_refresh.encode()).hexdigest()
        refresh = RefreshToken(
            tenant_id=tenant.id,
            token_hash=token_hash,
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        await self.token_repo.create(refresh)
        return TokenResponse(access_token=access_token, refresh_token=raw_refresh)

    async def logout(self, refresh_token: str) -> None:
        token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
        await self.token_repo.revoke(token_hash)
