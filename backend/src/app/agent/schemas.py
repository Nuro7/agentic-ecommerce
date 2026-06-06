"""Pydantic schemas for structured LLM responses and agent output validation.

Phase 6 — Structured LLM Output:
  LLMRawResponse  validates the raw dict that route_and_call returns.
  AgentResponse   validates the final response dict the orchestrator builds
                  before it reaches the HTTP / WebSocket layer.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, field_validator


# ═══════════════════════════════════════════════════════════════════════════════
# LLM RAW RESPONSE  (one tool-call round)
# ═══════════════════════════════════════════════════════════════════════════════

class ToolCallSchema(BaseModel):
    """One tool call emitted by the LLM."""
    id: str
    name: str
    arguments: Dict[str, Any] = {}

    @field_validator("id", "name", mode="before")
    @classmethod
    def coerce_str(cls, v: Any) -> str:
        return str(v) if v is not None else ""

    @field_validator("arguments", mode="before")
    @classmethod
    def coerce_arguments(cls, v: Any) -> Dict[str, Any]:
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        return v if isinstance(v, dict) else {}


class LLMRawResponse(BaseModel):
    """Validated shape returned by every route_and_call provider."""
    text: str = ""
    tool_calls: Optional[List[ToolCallSchema]] = None
    llm_route: str = "unknown"

    @field_validator("text", mode="before")
    @classmethod
    def coerce_text(cls, v: Any) -> str:
        return str(v) if v is not None else ""

    @field_validator("tool_calls", mode="before")
    @classmethod
    def coerce_tool_calls(cls, v: Any) -> Optional[List[dict]]:
        if not v or not isinstance(v, list):
            return None
        # filter out non-dict entries quietly
        return [tc for tc in v if isinstance(tc, dict)] or None

    @field_validator("llm_route", mode="before")
    @classmethod
    def coerce_route(cls, v: Any) -> str:
        return str(v) if v else "unknown"

    def to_dict(self) -> Dict[str, Any]:
        """Return a plain dict compatible with existing orchestrator code."""
        return {
            "text": self.text,
            "tool_calls": (
                [tc.model_dump() for tc in self.tool_calls]
                if self.tool_calls
                else None
            ),
            "llm_route": self.llm_route,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT RESPONSE  (final output from orchestrator)
# ═══════════════════════════════════════════════════════════════════════════════

class AgentResponse(BaseModel):
    """Validated final response that the orchestrator returns to callers."""
    response_text: str
    ui_actions: List[Dict[str, Any]] = []
    suggested_replies: List[str] = []
    last_products: List[Any] = []
    customer_email: Optional[str] = None
    llm_route: str = "unknown"

    @field_validator("response_text", mode="before")
    @classmethod
    def coerce_response_text(cls, v: Any) -> str:
        if v is None:
            return ""
        return str(v).strip()

    @field_validator("ui_actions", mode="before")
    @classmethod
    def coerce_actions(cls, v: Any) -> List[Dict[str, Any]]:
        if not isinstance(v, list):
            return []
        return [a for a in v if isinstance(a, dict)]

    @field_validator("suggested_replies", mode="before")
    @classmethod
    def coerce_replies(cls, v: Any) -> List[str]:
        if not isinstance(v, list):
            return []
        return [str(s) for s in v if s]

    @field_validator("last_products", mode="before")
    @classmethod
    def coerce_last_products(cls, v: Any) -> List[Any]:
        return v if isinstance(v, list) else []

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()
