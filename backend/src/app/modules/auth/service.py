import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from .repository import RefreshTokenRepository, MagicTokenRepository
from .models import RefreshToken, MagicToken
from .schemas import (
    LoginRequest, TokenResponse, RefreshRequest,
    AuthStatusRequest, AuthStatusResponse,
    MagicRequest, MagicResponse, MagicVerifyRequest, MagicVerifyResponse,
    SetPasswordRequest,
)
from ..tenants.repository import TenantRepository
from ...core.security import verify_password, hash_password, create_access_token
from ...core.exceptions import UnauthorizedError
from ...core.mailer import send_magic_link
from ...config import settings

logger = logging.getLogger(__name__)

# Pre-computed once at import so login does equal argon2 work whether or not the
# account exists — prevents user-enumeration via response timing.
_DUMMY_HASH = hash_password("speako-invalid-account")

_MAGIC_TTL = timedelta(minutes=15)


def _as_utc(dt: datetime) -> datetime:
    """Normalise a stored DB timestamp to UTC-aware for expiry comparison.
    SQLite returns naive datetimes; Postgres returns aware ones."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class AuthService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.token_repo = RefreshTokenRepository(db)
        self.magic_repo = MagicTokenRepository(db)
        self.tenant_repo = TenantRepository(db)

    # ── Email + password login (existing) ───────────────────────────────────

    async def login(self, data: LoginRequest) -> TokenResponse:
        tenant = await self.tenant_repo.get_by_email(data.email)
        # Fail closed and run a verify even when the tenant/hash is missing, so a
        # missing account is indistinguishable (timing) from a wrong password.
        stored_hash = getattr(tenant, "hashed_password", None) if tenant else None
        if not verify_password(data.password, stored_hash or _DUMMY_HASH):
            raise UnauthorizedError()
        if not tenant or not tenant.is_active or not stored_hash:
            raise UnauthorizedError()
        return await self._issue_tokens(tenant.id, tenant.email)

    async def logout(self, refresh_token: str) -> None:
        token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
        await self.token_repo.revoke(token_hash)

    # ── Dashboard adaptive login: status + magic link + set password ────────

    async def auth_status(self, data: AuthStatusRequest) -> AuthStatusResponse:
        """Lets the dashboard render the right login screen (password box vs
        magic-link) without ever leaking that an account exists to outsiders —
        a non-existent email and an existing one with no password both return
        has_password=False; only `recognized` distinguishes them."""
        tenant = await self.tenant_repo.get_by_email(str(data.email).lower())
        if not tenant or not tenant.is_active:
            return AuthStatusResponse(recognized=False, has_password=False)
        return AuthStatusResponse(recognized=True, has_password=bool(tenant.hashed_password))

    async def request_magic(self, data: MagicRequest) -> MagicResponse:
        tenant = await self.tenant_repo.get_by_email(str(data.email).lower())
        if not tenant or not tenant.is_active:
            # Don't reveal whether the account exists — return the same envelope.
            return MagicResponse(sent=False, dev_link=None)

        raw = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        magic = MagicToken(
            tenant_id=tenant.id,
            token_hash=token_hash,
            expires_at=datetime.now(timezone.utc) + _MAGIC_TTL,
            created_at=datetime.now(timezone.utc),
        )
        await self.magic_repo.create(magic)

        base = (settings.dashboard_url or settings.backend_url).strip().rstrip("/")
        if not base:
            logger.warning("No dashboard_url/backend_url configured — magic link unusable")
            return MagicResponse(sent=False, dev_link=None)
        link = f"{base}/auth/verify?token={raw}"

        sent = send_magic_link(tenant.email, link)
        dev_link = link if not sent else None
        return MagicResponse(sent=sent, dev_link=dev_link)

    async def verify_magic(self, data: MagicVerifyRequest) -> MagicVerifyResponse:
        raw = data.token.strip()
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        magic = await self.magic_repo.get_by_hash(token_hash)
        now = datetime.now(timezone.utc)
        if (
            not magic
            or magic.used_at is not None
            or _as_utc(magic.expires_at) < now
        ):
            raise UnauthorizedError()

        tenant = await self.tenant_repo.get_by_id(magic.tenant_id)
        if not tenant or not tenant.is_active:
            raise UnauthorizedError()

        # One-time use.
        await self.magic_repo.mark_used(magic.id, now)
        await self.db.commit()

        tokens = await self._issue_tokens(tenant.id, tenant.email)
        return MagicVerifyResponse(
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            needs_password=not bool(tenant.hashed_password),
        )

    async def set_password(
        self, tenant_id: str, data: SetPasswordRequest
    ) -> None:
        """Set/change the merchant's password. Caller must be authenticated (the
        tenant was resolved from a valid JWT or just-completed magic login)."""
        if data.password != data.confirm_password:
            from ...core.exceptions import ValidationError
            raise ValidationError("Passwords do not match")
        tenant = await self.tenant_repo.get_by_id(tenant_id)
        if not tenant:
            raise UnauthorizedError()
        tenant.hashed_password = hash_password(data.password)
        await self.tenant_repo.update(tenant)

    async def refresh(self, data: RefreshRequest) -> TokenResponse:
        """Rotate a refresh token: validate, revoke the old one, issue a new pair."""
        token_hash = hashlib.sha256(data.refresh_token.encode()).hexdigest()
        stored = await self.token_repo.get_by_hash(token_hash)
        if (
            not stored
            or _as_utc(stored.expires_at) < datetime.now(timezone.utc)
        ):
            raise UnauthorizedError()

        tenant = await self.tenant_repo.get_by_id(stored.tenant_id)
        if not tenant or not tenant.is_active:
            raise UnauthorizedError()

        await self.token_repo.revoke(token_hash)
        return await self._issue_tokens(tenant.id, tenant.email)

    # ── Helpers ─────────────────────────────────────────────────────────────

    async def _issue_tokens(self, tenant_id: str, email: str) -> TokenResponse:
        access_token = create_access_token({"sub": tenant_id, "email": email})
        raw_refresh = secrets.token_urlsafe(48)
        token_hash = hashlib.sha256(raw_refresh.encode()).hexdigest()
        refresh = RefreshToken(
            tenant_id=tenant_id,
            token_hash=token_hash,
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        await self.token_repo.create(refresh)
        return TokenResponse(access_token=access_token, refresh_token=raw_refresh)
