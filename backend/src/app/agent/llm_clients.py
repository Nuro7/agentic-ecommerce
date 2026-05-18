"""
Centralised LLM client setup — 4-way hybrid router.
Keys read from environment; missing keys degrade gracefully.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# ── Groq ──────────────────────────────────────────────────────────────────────
_groq_key = os.environ.get("GROQ_API_KEY", "")
groq_client = None
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

if _groq_key:
    try:
        from groq import AsyncGroq
        groq_client = AsyncGroq(api_key=_groq_key, max_retries=0, timeout=8.0)
        logger.info("LLM client: Groq (%s) ready", GROQ_MODEL)
    except Exception as e:
        logger.warning("Groq client init failed: %s", e)
else:
    logger.info("GROQ_API_KEY not set — Groq path disabled")

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

# ── Gemini 2.0 Flash ──────────────────────────────────────────────────────────
_gemini_key = os.environ.get("GEMINI_API_KEY", "")
gemini_client = None
GEMINI_MODEL = "gemini-2.0-flash"

if _gemini_key:
    try:
        from google import genai
        gemini_client = genai.Client(api_key=_gemini_key)
        logger.info("LLM client: Gemini 2.0 Flash ready")
    except Exception as e:
        logger.warning("Gemini client init failed: %s", e)
else:
    logger.info("GEMINI_API_KEY not set — Gemini path disabled")

# ── Routing config ────────────────────────────────────────────────────────────
DRAVIDIAN_LANGS: set[str] = set(os.environ.get("GEMINI_DRAVIDIAN_LANGS", "ml,ta,te,kn").split(","))
GPT_MINI_TOOL_THRESHOLD: int = int(os.environ.get("GPT_MINI_TOOL_THRESHOLD", "3"))
CART_KEYWORDS: frozenset[str] = frozenset({
    "cart", "coupon", "checkout", "address", "order",
    "add", "remove", "quantity", "pincode", "delivery",
    "cart mein", "add karo", "address dena", "order karna",
    "cart il", "address", "order cheyyuka",
})

ANY_LLM_AVAILABLE: bool = any(c is not None for c in (groq_client, gpt_mini_client, gemini_client))
