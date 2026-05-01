WooAgent — Product Requirements Document
Version: 2.0 — Hybrid Model
Date: March 2026
Status: Production-Ready

1. Product Overview
WooAgent is a voice-first AI shopping assistant embedded inside any WooCommerce store as a WordPress plugin. Customers interact with it by speaking or typing — the agent searches products, answers questions, manages the cart, applies coupons, and guides the customer through checkout — all in natural conversation, in their own language.

It is built specifically for Indian retail: supports 9 Indian languages, uses Indian English speech rhythms, understands Hinglish, handles Indian payment methods (UPI, COD), and uses Indian currency symbols natively.

2. Architecture Overview

WordPress Store (Frontend)
│
├── wooagent/widget/          ← Self-contained JS widget (Shadow DOM, zero deps)
│   ├── Voice input (Web Speech API / Groq Whisper)
│   ├── Orb UI + live transcript overlay
│   └── Chat bubble + product cards
│
├── wooagent/ (PHP plugin)    ← WP integration, REST proxy, HMAC signing
│
└── REST API (HMAC-signed) ──► FastAPI Backend
                                │
                                ├── agent/orchestrator.py   ← Brain of the agent
                                ├── services/llm_router.py  ← 4-way LLM routing
                                ├── services/wc_cache.py    ← Redis-cached WC proxy
                                ├── services/tts_service_v2.py ← Google TTS
                                ├── services/stt.py         ← Groq Whisper / Deepgram
                                ├── services/session.py     ← Session state
                                ├── services/session_facts.py ← Preference memory
                                └── services/beta_logger.py ← PostgreSQL telemetry
3. Technology Stack
3.1 Backend
Layer	Technology	Purpose
Framework	FastAPI (Python 3.12, async)	REST API, lifespan management
Cache	Redis (aioredis)	Session state, TTS cache (5000 entries), WC API cache
Database	PostgreSQL (asyncpg)	Beta session telemetry (optional)
Rate limiting	SlowAPI	Per-IP endpoint throttling
Security	HMAC-SHA256	Frontend-backend request verification
3.2 LLM Providers (4-Way Hybrid)
Provider	Model	Used For	Timeout
Groq	LLaMA 3.3 70B Versatile	Hindi, English, Bengali, Gujarati, Punjabi — fast path	8s
OpenAI	GPT-4o-mini	Cart, checkout, address FSM, tool-heavy queries	12s
OpenAI	GPT-4o	Escalation — complex edge cases (~5% of sessions)	15s
Google Gemini	Gemini 2.0 Flash	Dravidian languages (Malayalam, Tamil, Telugu, Kannada)	7s
Routing Logic (priority order):

escalate=True → GPT-4o
Address FSM active or cart keywords → GPT-4o-mini
Tool count ≥ 3 → GPT-4o-mini
Language in {ml, ta, te, kn} + ≤1 tool → Gemini 2.0 Flash
Language in {hi, en, bn, gu, pa} + ≤2 tools → Groq LLaMA 3.3
Default → GPT-4o-mini
All providers fall back gracefully. If Gemini fails → GPT-mini. If Groq fails → GPT-mini. If all OpenAI fails → Groq. The agent never returns an error to the user if any LLM is available.

3.3 Speech-to-Text (STT)
Mode	Provider	Model	Notes
Live (browser)	Web Speech API	Browser native	Zero latency, English-only reliable
Recorded	Groq Whisper	whisper-large-v3-turbo	Primary — multilingual, fast
Recorded fallback	Deepgram	nova-2	Auto-detected language
STT returns (transcript, confidence, language). Detected language from Whisper updates the session language for TTS routing.

3.4 Text-to-Speech (TTS)
Provider: Google Cloud TTS (primary for all languages)
Format: MP3, 24kHz, headphone-class EQ

Language	Voice	Type
English	en-IN-Journey-F	Journey (most conversational)
Hindi	hi-IN-Neural2-C	Neural2 (warmest)
Malayalam	ml-IN-Wavenet-A	Wavenet
Tamil	ta-IN-Neural2-A	Neural2
Telugu	te-IN-Neural2-A	Neural2
Kannada	kn-IN-Wavenet-A	Wavenet
Bengali	bn-IN-Wavenet-A	Wavenet
Gujarati	gu-IN-Wavenet-A	Wavenet
Punjabi	pa-IN-Wavenet-B	Wavenet
Speech naturalness features:

SSML with 350ms pauses after .!?, 150ms after ,;, 400ms for ...
Speaking rate 0.92 via SSML prosody
Redis cache: 5,000 entries, 24h TTL (common phrases never re-synthesized)
Fallback chain: Google → ElevenLabs → Groq TTS → Browser SpeechSynthesis
3.5 WooCommerce API
Accessed via a Redis-cached proxy (CachedWooCommerceClient) wrapping WooCommerceClient:

Resource	TTL	Reason
Product search	5 min	Products change occasionally
Product details	10 min	Rarely change mid-session
Variants	10 min	Stable
Inventory/stock	2 min	Sells fast
Categories	1 hour	Very stable
Store info	1 hour	Rarely changes
Reviews	30 min	Semi-stable
Cart, orders	Never	Always live
Pre-warm on startup: categories + 20 sample in-stock products loaded into Redis before the first user query.

3.6 Frontend Widget
Shadow DOM — zero style leakage, works on any WP theme
Single JS file — no dependencies, self-contained
Orb UI — animated orb shows state: idle / listening / processing / speaking
Live transcript — shown in orbHint element below orb during recording
Browser TTS rate — 0.82 (natural human pace, down from 1.05)
450ms mic restart delay after TTS ends — prevents picking up speaker echo
4. Core Features
4.1 Product Discovery
Natural language search: "show me red kurtas under ₹500"
Brand-aware search with honest "we don't carry that brand" fallback
Category browsing with get_categories() before any search
Compare up to 2 products with a real recommendation ("I'd go with X because Y")
Never lists more than 1 product per voice response
4.2 Product Variants & Inventory
find_variants() always called before add-to-cart — never assumes size/color
check_inventory() with exact attribute matching (color + size combination)
Low stock urgency: "just 3 left in your size"
Out-of-stock: immediate alternative product suggestion
4.3 Cart Management
Add single or multiple items (add_to_cart, add_multiple_to_cart)
Remove items, update quantities
Live cart display in widget — item count badge, mini cart view
Cart context sent with every message — agent always knows what's in the cart
4.4 Checkout Flow
Trigger: hard checkout keywords ("place order", "proceed to checkout")
get_best_coupon() always called before redirect — never sends to checkout empty-handed
Address FSM: 9-state finite state machine (name → address → city → state → pincode → phone → email → confirm → complete)
Supports "skip" for optional email
Speech-digit normalization: "four five six seven" → "4567"
India state normalization: "UP" → "Uttar Pradesh"
Redirects to pre-filled WooCommerce checkout with billing + shipping
4.5 Multilingual Support
Language	Script Detection	STT	LLM	TTS
English	Latin + Hinglish keywords	Whisper	Groq / GPT-mini	Journey-F
Hindi	Devanagari Unicode	Whisper	Groq	Neural2-C
Malayalam	Malayalam Unicode	Whisper	Gemini 2.0 Flash	Wavenet-A
Tamil	Tamil Unicode	Whisper	Gemini 2.0 Flash	Neural2-A
Telugu	Telugu Unicode	Whisper	Gemini 2.0 Flash	Neural2-A
Kannada	Kannada Unicode	Whisper	Gemini 2.0 Flash	Wavenet-A
Bengali	Bengali Unicode	Whisper	Groq	Wavenet-A
Gujarati	Gujarati Unicode	Whisper	Groq	Wavenet-A
Punjabi	Gurmukhi Unicode	Whisper	Groq	Wavenet-B
Language detection is instant — pure Unicode regex, no API call needed. Session language is updated on every turn from STT output.

4.6 Session Memory & Facts
SessionFactsService extracts and persists customer preferences across turns:

preferred_size — detected from messages (XS/S/M/L/XL or numeric)
preferred_color — color word detection (English + Hindi romanised)
max_budget — extracted from "under ₹500", "budget 1000", "upto 2k"
last_product_id / last_product_name — from tool results
Stored in Redis with 2-hour TTL. Injected into system prompt as:

"Customer preferences — size preference: M, color preference: blue, budget ≤ ₹800."

4.7 Fast-Intent Pre-LLM Router
Deterministic handler for simple intents — zero LLM cost:

"show my cart" / "what's in my cart" → get_cart() directly
"store hours" / "shipping policy" → get_store_info() directly
"remove [item]" → remove_from_cart() directly
Runs before any LLM is invoked. LLM only called if deterministic handler returns no result.

5. API Endpoints
Method	Path	Description
POST	/chat	Main conversation endpoint
POST	/greet	Session init + greeting with TTS
POST	/transcribe	Audio → transcript (STT)
GET	/health	Service health check
All endpoints also available at /api/v1/* prefix for backward compatibility.

Security: All requests verified via HMAC-SHA256 signature computed from SHARED_SECRET + timestamp + body. required=False in current mode — logs warning if missing but does not reject.

6. Agent Behavior Rules (Voice-First Guardrails)
Max 3 sentences per response — hard limit, no exceptions
Never list products — mention 1, the best one, then ask a question
Never hallucinate — every product detail must come from a tool call
No markdown — no bullet points, asterisks, numbered lists (spoken aloud)
No filler phrases — never say "Certainly!", "Great!", "Based on the search results"
Always end with a question or clear next step — never trail off
Always call find_variants before add-to-cart — never assume size/color
Always call get_best_coupon before checkout redirect
Strip <think> blocks — model reasoning never reaches TTS
Response capped at 4 sentences in post-processing even if LLM exceeds limit
7. Session & State Management

Redis Keys:
  session:<id>          → conversation history, cart snapshot, last products (2h TTL)
  session_meta:<id>     → language, address state, greeted flag (2h TTL)
  session_facts:<id>    → size/color/budget preferences (2h TTL)
  wc:<type>:<store>:<hash> → WooCommerce API cache (per-resource TTL)
  tts:<md5hash>         → Synthesized audio base64 (24h TTL)
All services fall back to in-process Python dicts if Redis is unavailable. No crash, no data loss — just no cross-process sharing.

8. Beta Telemetry
BetaLogger writes to PostgreSQL beta_sessions table (opt-in via BETA_LOGGING_ENABLED=true):

Column	Description
session_id	Unique session identifier
store_id	Store URL hash
language	Detected language
turns	Total conversation turns
tool_calls	Total tool invocations
route_groq/gemini/gpt_mini/gpt4o	Turn count per LLM route
cart_value	Peak cart value during session
checkout_reached	Whether checkout was triggered
started_at / updated_at	Timestamps
Used to measure: conversion rate, LLM cost distribution, language breakdown, average session depth.

9. Configuration Reference

# Store
WOOCOMMERCE_STORE_URL=
WOOCOMMERCE_CONSUMER_KEY=
WOOCOMMERCE_CONSUMER_SECRET=
SHARED_SECRET=                     # HMAC key — generate with secrets.token_hex(32)

# LLM
GROQ_API_KEY=                      # Fast path — Hindi/English + STT
OPENAI_API_KEY=                    # Cart/checkout — GPT-4o-mini + GPT-4o
GEMINI_API_KEY=                    # Dravidian languages

# STT
STT_PROVIDER=groq                  # groq | deepgram
GROQ_STT_MODEL=whisper-large-v3-turbo

# TTS
TTS_PROVIDER=google
GOOGLE_TTS_API_KEY=

# Infrastructure
REDIS_URL=redis://localhost:6379
ALLOWED_ORIGINS=https://yourstore.com
LOG_LEVEL=INFO

# Optional features
BETA_LOGGING_ENABLED=false
DATABASE_URL=postgresql+asyncpg://...
MVP_MODE=true
10. Deployment
Dependencies:


fastapi, uvicorn, httpx, redis, pydantic, groq,
openai, google-generativeai, asyncpg, pgvector,
slowapi, python-dotenv, python-multipart
Start:


cd wooagent-backend
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
Docker: docker-compose.yml included for containerized deployment with Redis.

Minimum viable setup (works with only Groq key): Hindi + English conversations, all cart/checkout features degraded to Groq fallback.

Recommended setup: All 3 LLM keys + Google TTS + Redis for full multilingual voice experience.