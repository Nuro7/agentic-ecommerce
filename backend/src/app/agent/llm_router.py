"""
4-way LLM routing for the agent.

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

from .schemas import LLMRawResponse
from .llm_clients import (
    groq_client, GROQ_MODEL,
    gpt_mini_client, GPT_MINI_MODEL,
    gpt4o_client, GPT4O_MODEL,
    gemini_client, BRAIN_MODEL,
    GPT_MINI_TOOL_THRESHOLD, CART_KEYWORDS,
)
from ..core.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)

# Per-provider circuit breakers: after repeated failures/timeouts we skip a
# provider for a cooldown instead of paying its full timeout on every request
# (which would stall every fallback below it). 30s recovery, then a probe.
_grok_breaker = CircuitBreaker(name="llm-grok", failure_threshold=3, recovery_timeout=30.0)
_gpt_mini_breaker = CircuitBreaker(name="llm-gpt-mini", failure_threshold=3, recovery_timeout=30.0)
_gemini_breaker = CircuitBreaker(name="llm-gemini", failure_threshold=3, recovery_timeout=30.0)

# ── Latency budget ──────────────────────────────────────────────────────────
# Per-provider timeouts were 15/12/15s, so a slow primary could walk the whole
# fallback chain and burn ~40s on a single turn — the root of "sometimes it takes
# forever, I can't predict it". Tighter per-provider caps make failover snappy,
# and _TURN_LLM_DEADLINE bounds the TOTAL wall-clock across the chain so one
# route_and_call can never exceed it regardless of how many providers it tries.
_PRIMARY_TIMEOUT = 9.0
_FALLBACK_TIMEOUT = 8.0
_ESCALATION_TIMEOUT = 13.0
_TURN_LLM_DEADLINE = 16.0   # hard ceiling for the whole provider chain, per call


def _budget_timeout(start: float, cap: float) -> float:
    """Time left before the per-call deadline, capped at the provider's own limit.
    Never returns < 1.0s so the final provider still gets a real attempt."""
    remaining = _TURN_LLM_DEADLINE - (asyncio.get_event_loop().time() - start)
    return max(1.0, min(cap, remaining))


# ── Response validator ─────────────────────────────────────────────────────

def _validated_response(raw: dict) -> dict:
    """Parse a provider dict through LLMRawResponse and return a clean dict.

    On validation failure the raw dict is returned unchanged so the caller
    always gets something — a malformed response is better than a crash.
    """
    try:
        return LLMRawResponse.model_validate(raw).to_dict()
    except Exception as exc:
        logger.debug("LLMRawResponse validation failed (using raw): %s", exc)
        return raw


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
    availability_patterns = (
        "available", "in stock", "do you have", "is there", "have a", "have any",
        "looking for", "find me", "show me", "search for",
    )
    return any(p in msg_lower for p in availability_patterns)


def _estimate_tool_count(message: str) -> int:
    """Heuristic: estimate how many tool calls this message will trigger."""
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
    """xAI Grok via OpenAI-compatible API (GROK_API_KEY / grok-4.3)."""
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
        "llm_route": f"grok:{GROQ_MODEL}",
    }


async def _call_gemini(
    messages: list[dict], tools: list[dict]
) -> dict:
    gemini_tools = _openai_tools_to_gemini(tools)

    history = []
    last_user_msg = ""
    for m in messages:
        role = m.get("role", "")
        content = m.get("content") or ""
        if role == "system":
            last_user_msg = content + "\n\n" + last_user_msg
        elif role == "user":
            last_user_msg = content
            history.append({"role": "user", "parts": [content]})
        elif role == "assistant" and content:
            history.append({"role": "model", "parts": [content]})

    chat = gemini_client.start_chat(history=history[:-1] if len(history) > 1 else [])
    # Bound the blocking SDK call so a hung Gemini can't stall the turn (and leak
    # the worker thread) indefinitely.
    response = await asyncio.wait_for(
        asyncio.to_thread(
            chat.send_message,
            last_user_msg,
            tools=gemini_tools if gemini_tools else None,
        ),
        timeout=15.0,
    )

    text = ""
    tool_calls = []
    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            text = part.text
        elif hasattr(part, "function_call") and part.function_call:
            tool_calls.append({
                "id": str(uuid.uuid4())[:8],
                "name": part.function_call.name,
                "arguments": dict(part.function_call.args or {}),
            })

    return {
        "text": text,
        "tool_calls": tool_calls or None,
        "llm_route": "gemini",
    }


async def _call_gemini_brain(messages: list[dict], tools: list[dict]) -> dict:
    """
    Gemini 2.5 Flash as the Brain — uses generate_content (new google-genai SDK).
    Model is configurable via BRAIN_MODEL env var.
    Same GEMINI_API_KEY used for both this and Gemini Live voice.
    """
    from google.genai import types as gtypes

    system_instruction: str | None = None
    contents: list = []

    for m in messages:
        role    = m.get("role", "")
        content = m.get("content") or ""
        if role == "system":
            system_instruction = content
        elif role == "user":
            contents.append(
                gtypes.Content(role="user", parts=[gtypes.Part.from_text(text=content)])
            )
        elif role == "assistant" and content:
            contents.append(
                gtypes.Content(role="model", parts=[gtypes.Part.from_text(text=content)])
            )

    config_kwargs: dict = {"temperature": 0.1, "max_output_tokens": 512}
    if system_instruction:
        config_kwargs["system_instruction"] = system_instruction
    if tools:
        gemini_tools = _openai_tools_to_gemini(tools)
        config_kwargs["tools"] = [{"function_declarations": gemini_tools}]

    response = await asyncio.wait_for(
        gemini_client.aio.models.generate_content(
            model=BRAIN_MODEL,
            contents=contents,
            config=gtypes.GenerateContentConfig(**config_kwargs),
        ),
        timeout=15.0,
    )

    text = ""
    tool_calls: list[dict] = []

    if response.candidates:
        for part in response.candidates[0].content.parts:
            if hasattr(part, "text") and part.text:
                text = part.text
            elif hasattr(part, "function_call") and part.function_call:
                tool_calls.append({
                    "id":        str(uuid.uuid4())[:8],
                    "name":      part.function_call.name,
                    "arguments": dict(part.function_call.args or {}),
                })

    return {
        "text":       text,
        "tool_calls": tool_calls or None,
        "llm_route":  f"gemini-brain:{BRAIN_MODEL}",
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


# ── Best-available fallback ────────────────────────────────────────────────

async def _best_available(messages: list[dict], tools: list[dict]) -> dict:
    # Each call is timeout-bounded so a hung provider can't stall the turn
    # (this runs on the escalation fallback path, which has no outer wait_for).
    if groq_client:
        return await asyncio.wait_for(_call_groq(messages, tools), timeout=_FALLBACK_TIMEOUT)
    if gpt_mini_client:
        return await asyncio.wait_for(_call_gpt_mini(messages, tools), timeout=_FALLBACK_TIMEOUT)
    if gemini_client:
        return await asyncio.wait_for(_call_gemini_brain(messages, tools), timeout=_FALLBACK_TIMEOUT)
    raise RuntimeError("No LLM clients configured — set GROK_API_KEY, OPENAI_API_KEY, or GEMINI_API_KEY")


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
        address_active: Checkout address FSM is collecting fields — escalate for
                        reliable structured extraction.
        turn_count:     Conversation turn depth (reserved for future throttling).
        force_text:     Strip tools so the LLM generates spoken text, not more tool calls.
    """
    if force_text:
        tools = []

    # Address collection needs precise field extraction — escalate automatically
    if address_active:
        escalate = True

    tool_count = _estimate_tool_count(message_text)
    _t0 = asyncio.get_event_loop().time()

    if escalate:
        # Escalation: try GPT-4o first for maximum accuracy, fall back to Grok.
        # Bound the call with a timeout and fall back on failure — otherwise a
        # hung GPT-4o stalls the whole turn (escalation always fires during
        # checkout/address collection, so this path is hot).
        logger.info("LLM route: escalation [lang=%s]", lang)
        if gpt4o_client:
            try:
                raw = await asyncio.wait_for(
                    _call_gpt4o(messages, tools),
                    timeout=_budget_timeout(_t0, _ESCALATION_TIMEOUT),
                )
                return _validated_response(raw)
            except Exception as e:
                logger.warning("GPT-4o escalation failed (%s), falling back", e)
        return _validated_response(await _best_available(messages, tools))

    # ── Primary: xAI Grok (grok-4.3) ─────────────────────────────────────────
    # Flagship model — leading tool calling, low hallucination, 1M context.
    # Falls back to GPT-4o-mini → Gemini if Grok is unavailable.
    if groq_client and _grok_breaker.is_available():
        logger.info(
            "LLM route: xAI Grok [%s, lang=%s, tools~%d]",
            GROQ_MODEL, lang, tool_count,
        )
        try:
            raw = await asyncio.wait_for(
                _call_groq(messages, tools),
                timeout=_budget_timeout(_t0, _PRIMARY_TIMEOUT),
            )
            _grok_breaker.record_success()
            return _validated_response(raw)
        except Exception as e:
            _grok_breaker.record_failure()
            logger.warning("xAI Grok Brain failed (%s), falling back to GPT-4o-mini", e)

    if gpt_mini_client and _gpt_mini_breaker.is_available():
        logger.info("LLM route: GPT-4o-mini [fallback, lang=%s]", lang)
        try:
            raw = await asyncio.wait_for(
                _call_gpt_mini(messages, tools),
                timeout=_budget_timeout(_t0, _FALLBACK_TIMEOUT),
            )
            _gpt_mini_breaker.record_success()
            return _validated_response(raw)
        except Exception as e:
            _gpt_mini_breaker.record_failure()
            logger.warning("GPT-4o-mini fallback failed (%s), trying Gemini", e)

    if gemini_client and _gemini_breaker.is_available():
        logger.info("LLM route: Gemini [last-resort fallback, lang=%s]", lang)
        try:
            # Gemini had no timeout before — a hung call stalled the whole turn.
            raw = await asyncio.wait_for(
                _call_gemini_brain(messages, tools),
                timeout=_budget_timeout(_t0, _FALLBACK_TIMEOUT),
            )
            _gemini_breaker.record_success()
            return _validated_response(raw)
        except Exception as e:
            _gemini_breaker.record_failure()
            logger.warning("Gemini last-resort failed (%s)", e)

    raise RuntimeError("All LLM backends failed/circuit-open — check GROK_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY")
