from pydantic import BaseModel, EmailStr
from datetime import datetime


class UserCreate(BaseModel):
    email: EmailStr
    name: str | None = None
    password: str
    role: str = "admin"


class UserUpdate(BaseModel):
    name: str | None = None
    role: str | None = None


class UserOut(BaseModel):
    id: str
    tenant_id: str
    email: str
    name: str | None
    role: str
    created_at: datetime

    model_config = {"from_attributes": True}
