from pydantic import BaseModel
from datetime import datetime


class MessageIn(BaseModel):
    role: str
    content: str


class MessageOut(BaseModel):
    id: str
    role: str
    content: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ConversationOut(BaseModel):
    id: str
    session_id: str
    channel: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatRequest(BaseModel):
    session_id: str
    message: str
    visitor_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    session_id: str
