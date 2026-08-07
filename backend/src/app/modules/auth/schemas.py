from pydantic import BaseModel, EmailStr


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class EmailLoginRequest(BaseModel):
    email: EmailStr


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class AuthStatusRequest(BaseModel):
    email: EmailStr


class AuthStatusResponse(BaseModel):
    recognized: bool
    has_password: bool


class MagicRequest(BaseModel):
    email: EmailStr


class MagicResponse(BaseModel):
    sent: bool
    # When SMTP is not configured we return the link so dev/tooling can use it
    # directly. Omitted (None) in production so a live mail server is required.
    dev_link: str | None = None


class MagicVerifyRequest(BaseModel):
    token: str


class MagicVerifyResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    needs_password: bool = False


class SetPasswordRequest(BaseModel):
    password: str
    confirm_password: str
