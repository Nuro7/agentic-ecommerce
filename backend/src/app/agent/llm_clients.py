"""
Centralised LLM client setup — 4-way hybrid router.
Keys read from environment; missing keys degrade gracefully.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# ── xAI Grok ──────────────────────────────────────────────────────────────────
# OpenAI-compatible API — uses AsyncOpenAI with xAI base URL.
# GROK_API_KEY  → xai-...  from console.x.ai
# GROK_MODEL    → LLM brain fallback model (default: grok-4.3)
# GROK_CLASSIFIER_MODEL → fast model for intent classification (default: grok-3-mini-fast)
_grok_key = os.environ.get("GROK_API_KEY", "")
xai_client = None
XAI_MODEL = os.environ.get("GROK_MODEL", "grok-4.3")
GROK_CLASSIFIER_MODEL = os.environ.get("GROK_CLASSIFIER_MODEL", "grok-3-mini-fast")

if _grok_key:
    try:
        from openai import AsyncOpenAI
        xai_client = AsyncOpenAI(
            api_key=_grok_key,
            base_url="https://api.x.ai/v1",
            max_retries=0,
            timeout=8.0,
        )
        logger.info("LLM client: xAI Grok (%s) ready", XAI_MODEL)
    except Exception as e:
        logger.warning("xAI Grok client init failed: %s", e)
else:
    logger.info("GROK_API_KEY not set — xAI Grok path disabled")

# ── OpenAI (GPT-4o-mini + GPT-4o) ─────────────────────────────────────────────
_openai_key = os.environ.get("OPENAI_API_KEY", "")
gpt_mini_client = None
gpt4o_client = None
GPT_MINI_MODEL = "gpt-4o-mini"
GPT4O_MODEL = "gpt-4o"

if _openai_key:
    try:
        from openai import AsyncOpenAI
        gpt_mini_client = AsyncOpenAI(api_key=_openai_key, max_retries=1, timeout=12.0)
        gpt4o_client    = AsyncOpenAI(api_key=_openai_key, max_retries=1, timeout=15.0)
        logger.info("LLM client: GPT-4o-mini + GPT-4o ready")
    except Exception as e:
        logger.warning("OpenAI client init failed: %s", e)
else:
    logger.info("OPENAI_API_KEY not set — GPT paths disabled")

# ── Gemini Brain ──────────────────────────────────────────────────────────────
# NOTE: BRAIN_MODEL sets the Gemini FALLBACK model only — it is NOT the overall
# primary brain. The primary brain is GPT-4o-mini; the routing order is
# GPT-4o-mini → Grok → Gemini (see llm_router.py). Gemini runs only if the first
# two are unavailable.
# BRAIN_MODEL controls which Gemini model powers that fallback reasoning path.
# Change via .env — no code changes needed.
#   gemini-2.5-flash   → fast, large context, best for agentic tasks (default)
#   gemini-2.0-flash   → previous generation, slightly lighter
#   gemini-2.5-pro     → highest quality, higher latency / cost
_gemini_key = os.environ.get("GEMINI_API_KEY", "")
gemini_client = None
BRAIN_MODEL   = os.environ.get("BRAIN_MODEL", "gemini-2.5-flash")
GEMINI_MODEL  = BRAIN_MODEL  # kept for backwards-compat references

if _gemini_key:
    try:
        from google import genai
        gemini_client = genai.Client(api_key=_gemini_key)
        logger.info("LLM client: Gemini Brain (%s) ready", BRAIN_MODEL)
    except Exception as e:
        logger.warning("Gemini client init failed: %s", e)
else:
    logger.info("GEMINI_API_KEY not set — Gemini Brain disabled")

# ── Routing config ────────────────────────────────────────────────────────────
DRAVIDIAN_LANGS: set[str] = set(os.environ.get("GEMINI_DRAVIDIAN_LANGS", "ml,ta,te,kn").split(","))
GPT_MINI_TOOL_THRESHOLD: int = int(os.environ.get("GPT_MINI_TOOL_THRESHOLD", "3"))
CART_KEYWORDS: frozenset[str] = frozenset({
    "cart", "coupon", "checkout", "address", "order",
    "add", "remove", "quantity", "pincode", "delivery",
    "cart mein", "add karo", "address dena", "order karna",
    "cart il", "address", "order cheyyuka",
})

ANY_LLM_AVAILABLE: bool = any(c is not None for c in (xai_client, gpt_mini_client, gemini_client))
