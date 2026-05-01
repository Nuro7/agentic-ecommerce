"""
services/llm_router.py
4-way LLM routing for WooAgent hybrid model.

Routing priority:
  1. escalate=True          → GPT-4o
  2. address FSM active     → GPT-4o-mini
  3. cart/coupon/multi-tool → GPT-4o-mini
  4. Dravidian lang + ≤1 tool → Gemini 2.0 Flash (→ GPT-mini fallback)
  5. Hindi/English simple   → Groq LLaMA 3.3 70B (→ GPT-mini fallback)
  6. Default                → GPT-4o-mini

Unified response format:
  {
    "text":       str,
    "tool_calls": [{"id": str, "name": str, "arguments": dict}] | None,
    "llm_route":  str,   # "groq" | "gemini" | "gpt-4o-mini" | "gpt-4o"
  }
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from services.llm_clients import (
    groq_client, GROQ_MODEL,
    gpt_mini_client, GPT_MINI_MODEL,
    gpt4o_client, GPT4O_MODEL,
    gemini_client,
    DRAVIDIAN_LANGS, GPT_MINI_TOOL_THRESHOLD, CART_KEYWORDS,
)

logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────

def _needs_gpt(message: str, address_active: bool, tool_count: int) -> bool:
    """Return True when this query must be handled by GPT-4o-mini."""
    if address_active:
        return True
    if tool_count >= GPT_MINI_TOOL_THRESHOLD:
        return True
    msg_lower = message.lower()
    if any(kw in msg_lower for kw in CART_KEYWORDS):
        return True
    # Product availability/search queries need reliable tool calling — always use GPT-mini
    # Groq sometimes skips tool calls for "is X available?" patterns
    availability_patterns = (
        "available", "in stock", "do you have", "is there", "have a", "have any",
        "looking for", "find me", "show me", "search for",
    )
    return any(p in msg_lower for p in availability_patterns)


def _estimate_tool_count(message: str) -> int:
    """
    Heuristic: estimate how many tool calls this message will trigger.
    Used for routing before any LLM is invoked.
    """
    msg = message.lower()
    count = 0
    if any(w in msg for w in ("search", "find", "show", "available", "have")):
        count += 1
    if any(w in msg for w in ("size", "colour", "color", "variant")):
        count += 1
    if any(w in msg for w in ("add", "cart", "buy", "purchase")):
        count += 2
    if any(w in msg for w in ("coupon", "discount", "offer", "code")):
        count += 1
    if any(w in msg for w in ("address", "pincode", "city", "deliver")):
        count += 2
    if any(w in msg for w in ("compare", "difference", "vs", "better")):
        count += 2
    return min(count, 6)


def _openai_tools_to_gemini(openai_tools: list[dict]) -> list[dict]:
    """Convert OpenAI tool schema format to Gemini FunctionDeclaration format."""
    return [
        {
            "name": t["function"]["name"],
            "description": t["function"]["description"],
            "parameters": t["function"].get("parameters", {}),
        }
        for t in openai_tools
        if isinstance(t, dict) and "function" in t
    ]


# ── Per-provider call functions ────────────────────────────────────────────

async def _call_groq(messages: list[dict], tools: list[dict]) -> dict:
    kwargs: dict = dict(model=GROQ_MODEL, messages=messages, temperature=0.2, max_tokens=512)
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    resp = await groq_client.chat.completions.create(**kwargs)
    msg = resp.choices[0].message
    tool_calls = [
        {
            "id": tc.id,
            "name": tc.function.name,
            "arguments": json.loads(tc.function.arguments or "{}"),
        }
        for tc in (msg.tool_calls or [])
    ]
    return {
        "text": msg.content or "",
        "tool_calls": tool_calls or None,
        "llm_route": "groq",
    }


async def _call_gemini(
    messages: list[dict], tools: list[dict]
) -> dict:
    gemini_tools = _openai_tools_to_gemini(tools)

    # Convert OpenAI message format → Gemini history
    history = []
    last_user_msg = ""
    for m in messages:
        role = m.get("role", "")
        content = m.get("content") or ""
        if role == "system":
            # Prepend system context to first user message
            last_user_msg = content + "\n\n" + last_user_msg
        elif role == "user":
            last_user_msg = content
            history.append({"role": "user", "parts": [content]})
        elif role == "assistant" and content:
            history.append({"role": "model", "parts": [content]})

    chat = gemini_client.start_chat(history=history[:-1] if len(history) > 1 else [])
    response = await asyncio.to_thread(
        chat.send_message,
        last_user_msg,
        tools=gemini_tools if gemini_tools else None,
    )

    text = ""
    tool_calls = []
    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            text = part.text
        elif hasattr(part, "function_call") and part.function_call:
            tool_calls.append({
                "id": str(uuid.uuid4())[:8],   # Gemini has no call ID — generate one
                "name": part.function_call.name,
                "arguments": dict(part.function_call.args),
            })

    return {
        "text": text,
        "tool_calls": tool_calls or None,
        "llm_route": "gemini",
    }


async def _call_gpt_mini(messages: list[dict], tools: list[dict]) -> dict:
    kwargs: dict = dict(model=GPT_MINI_MODEL, messages=messages, temperature=0.2, max_tokens=512)
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
        kwargs["parallel_tool_calls"] = True
    resp = await gpt_mini_client.chat.completions.create(**kwargs)
    msg = resp.choices[0].message
    tool_calls = [
        {
            "id": tc.id,
            "name": tc.function.name,
            "arguments": json.loads(tc.function.arguments or "{}"),
        }
        for tc in (msg.tool_calls or [])
    ]
    return {
        "text": msg.content or "",
        "tool_calls": tool_calls or None,
        "llm_route": "gpt-4o-mini",
    }


async def _call_gpt4o(messages: list[dict], tools: list[dict]) -> dict:
    kwargs: dict = dict(model=GPT4O_MODEL, messages=messages, temperature=0.1, max_tokens=768)
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
        kwargs["parallel_tool_calls"] = True
    resp = await gpt4o_client.chat.completions.create(**kwargs)
    msg = resp.choices[0].message
    tool_calls = [
        {
            "id": tc.id,
            "name": tc.function.name,
            "arguments": json.loads(tc.function.arguments or "{}"),
        }
        for tc in (msg.tool_calls or [])
    ]
    return {
        "text": msg.content or "",
        "tool_calls": tool_calls or None,
        "llm_route": "gpt-4o",
    }


# ── Best-available fallback (when preferred route's client is None) ────────

async def _best_available(messages: list[dict], tools: list[dict]) -> dict:
    """
    Called when the preferred LLM for a route isn't configured.
    Tries: GPT-mini → Groq → Gemini → raises
    """
    if gpt_mini_client:
        return await _call_gpt_mini(messages, tools)
    if groq_client:
        return await _call_groq(messages, tools)
    if gemini_client:
        return await _call_gemini(messages, tools)
    raise RuntimeError("No LLM clients configured — set GROQ_API_KEY, OPENAI_API_KEY, or GEMINI_API_KEY")


# ── Main routing entry point ───────────────────────────────────────────────

async def route_and_call(
    messages: list[dict],
    tools: list[dict],
    lang: str,
    address_active: bool,
    turn_count: int,
    message_text: str,
    escalate: bool = False,
    force_text: bool = False,
) -> dict:
    """
    Route a message to the best LLM and return a unified response dict.

    Args:
        force_text: When True, pass tool_choice="none" so the LLM must
                    generate a spoken response instead of calling more tools.
                    Use this on the final synthesis round after tools have run.

    Returns:
        {
          "text":       str,
          "tool_calls": [{"id", "name", "arguments"}] | None,
          "llm_route":  str,
        }
    """
    # When forced to text, pass empty tools so no provider can call any.
    # This is simpler than per-provider tool_choice overrides.
    if force_text:
        tools = []

    tool_count = _estimate_tool_count(message_text)

    # ── Route 1: Escalation → GPT-4o ──────────────────────────────────────
    if escalate:
        logger.info("LLM route: GPT-4o [escalation]")
        if gpt4o_client:
            return await _call_gpt4o(messages, tools)
        logger.warning("GPT-4o not available, falling back to best available")
        return await _best_available(messages, tools)

    # ── Route 2 & 3: Address FSM / cart / multi-tool → GPT-4o-mini ────────
    if _needs_gpt(message_text, address_active, tool_count):
        logger.info(
            "LLM route: GPT-4o-mini [tool-heavy/address/cart, lang=%s, tools~%d]",
            lang, tool_count,
        )
        if gpt_mini_client:
            try:
                return await asyncio.wait_for(_call_gpt_mini(messages, tools), timeout=12.0)
            except Exception as e:
                logger.warning("GPT-4o-mini failed (%s), trying Groq fallback", e)
                if groq_client:
                    return await _call_groq(messages, tools)
                raise
        return await _best_available(messages, tools)

    # ── Route 4: Default → GPT-4o-mini ────────────────────────────────────
    # GPT-4o-mini is the primary model — fast, reliable tool calling, multilingual.
    # Groq / Gemini used only as fallback if mini is unavailable.
    logger.info("LLM route: GPT-4o-mini [primary, lang=%s, tools~%d]", lang, tool_count)
    if gpt_mini_client:
        try:
            return await asyncio.wait_for(_call_gpt_mini(messages, tools), timeout=12.0)
        except Exception as e:
            logger.warning("GPT-4o-mini failed (%s), trying fallback", e)
            if groq_client:
                try:
                    return await asyncio.wait_for(_call_groq(messages, tools), timeout=8.0)
                except Exception as e2:
                    logger.warning("Groq fallback also failed (%s)", e2)
            if gemini_client:
                return await _call_gemini(messages, tools)
            raise
    return await _best_available(messages, tools)
