from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from .service import AuthService
from .schemas import (
    LoginRequest, TokenResponse, RefreshRequest,
    AuthStatusRequest, AuthStatusResponse,
    MagicRequest, MagicResponse, MagicVerifyRequest, MagicVerifyResponse,
    SetPasswordRequest,
)
from ...core.database import get_db
from ..tenants.dependencies import get_authenticated_tenant

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(data: LoginRequest, db: AsyncSession = Depends(get_db)):
    return await AuthService(db).login(data)


@router.post("/logout", status_code=204)
async def logout(data: RefreshRequest, db: AsyncSession = Depends(get_db)):
    await AuthService(db).logout(data.refresh_token)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(data: RefreshRequest, db: AsyncSession = Depends(get_db)):
    return await AuthService(db).refresh(data)


@router.post("/status", response_model=AuthStatusResponse)
async def auth_status(data: AuthStatusRequest, db: AsyncSession = Depends(get_db)):
    return await AuthService(db).auth_status(data)


@router.post("/magic-request", response_model=MagicResponse)
async def magic_request(data: MagicRequest, db: AsyncSession = Depends(get_db)):
    return await AuthService(db).request_magic(data)


@router.post("/magic-verify", response_model=MagicVerifyResponse)
async def magic_verify(data: MagicVerifyRequest, db: AsyncSession = Depends(get_db)):
    return await AuthService(db).verify_magic(data)


@router.post("/set-password", status_code=204)
async def set_password(
    data: SetPasswordRequest,
    tenant=Depends(get_authenticated_tenant),
    db: AsyncSession = Depends(get_db),
):
    await AuthService(db).set_password(tenant.id, data)
