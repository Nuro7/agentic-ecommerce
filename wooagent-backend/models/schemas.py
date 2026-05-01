from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class CurrentPage(BaseModel):
    url: Optional[str] = None
    title: Optional[str] = None
    product_id: Optional[int] = None
    product_name: Optional[str] = None


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=500)
    message_type: Literal["text", "voice_transcript"] = "text"
    store_url: Optional[str] = None
    store_name: Optional[str] = None
    currency: Optional[str] = None
    language: Optional[str] = None
    conversation_history: List[Dict[str, Any]] = Field(default_factory=list)
    cart_context: Dict[str, Any] = Field(default_factory=dict)
    current_page: Optional[CurrentPage] = None

    @field_validator("cart_context", mode="before")
    @classmethod
    def _coerce_cart_context(cls, v: Any) -> Dict[str, Any]:
        """WooCommerce/widget sometimes sends [] instead of {} — silently coerce."""
        if isinstance(v, dict):
            return v
        return {}


class Action(BaseModel):
    type: str
    payload: Dict[str, Any] = Field(default_factory=dict)


class ChatResponse(BaseModel):
    session_id: str
    text: str
    response_text: Optional[str] = None
    audio_base64: Optional[str] = None
    audio_format: Optional[str] = None
    tts_fallback: Optional[str] = None
    speech_text: Optional[str] = None
    language: str = "en"
    ui_actions: List[Action] = Field(default_factory=list)
    actions: List[Action] = Field(default_factory=list)
    address_state: str = "idle"
    suggested_replies: List[str] = Field(default_factory=list)


class TranscribeResponse(BaseModel):
    transcript: str
    confidence: float = 0.0
    language: str = "unknown"
    error: Optional[str] = None
