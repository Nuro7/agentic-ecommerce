from pydantic import BaseModel, EmailStr
from datetime import datetime


class TenantCreate(BaseModel):
    name: str
    email: EmailStr
    plan: str = "free"


class TenantUpdate(BaseModel):
    name: str | None = None
    plan: str | None = None
    is_active: bool | None = None


class TenantOut(BaseModel):
    id: str
    name: str
    email: str
    plan: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}
