from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from .service import UserService
from .schemas import UserCreate, UserOut
from ...core.database import get_db
from ..tenants.dependencies import require_tenant

router = APIRouter(prefix="/users", tags=["users"])


@router.post("/", response_model=UserOut, status_code=201)
async def create_user(
    data: UserCreate,
    tenant=Depends(require_tenant),
    db: AsyncSession = Depends(get_db),
):
    return await UserService(db).create_user(tenant.id, data)


@router.get("/", response_model=list[UserOut])
async def list_users(tenant=Depends(require_tenant), db: AsyncSession = Depends(get_db)):
    return await UserService(db).list_users(tenant.id)
