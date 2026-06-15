# Speako — Voice Concurrency Load Test (10 merchants × up to 20 customers)

**Goal:** can the **voice** pipeline handle 10 merchants each with ~20 simultaneous
customers (≈200 concurrent voice sessions)?

**Method:** 10 merchants onboarded + upgraded to Growth (voice) + synced. Each
"customer" = one voice WS session (`/wooagent/stream`) driven by `text_input`,
exercising the real **Gemini Live session → brain → TTS audio** path; we measure
whether **TTS audio** is produced. Concurrency ramped 10→25→50→100→200 across the
10 merchants; voice rate-limit temporarily raised (single harness IP). 
**Not tested:** mic→STT accuracy (can't be automated — needs human/browser).

## Result — voice AUDIO collapses under concurrency

| Concurrent | Got a reply | **Produced voice audio (TTS)** | p50 lat | p95 lat | mem | health |
|---|---|---|---|---|---|---|
| 10  | 10/10  (100%) | **10/10 (100%)** | 18.0 s | 18.9 s | low | ok |
| 25  | 25/25  (100%) | **25/25 (100%)** | 17.1 s | 18.7 s | low | ok |
| 50  | 50/50  (100%) | **50/50 (100%)** | 12.4 s | 14.0 s | ~2.3 GB | ok |
| 100 | 100/100 (100%) | **75/100 (75%)** ⚠️ | 14.3 s | 16.6 s | ~2.3 GB | ok |
| 200 | 200/200 (100%) | **1/200 (~0%)** 🔴 | 14.4 s | 18.8 s | ~2.4 GB | ok |

## What this means

### 🔴 Finding 1 — Voice (TTS audio) does NOT scale to the target load
At **200 concurrent** (10×20), **199 of 200 customers got a text reply but NO spoken
audio**. Voice is solid to **~50 concurrent**, starts dropping at **100 (75%)**, and
**effectively collapses by 200 (~0%)**. The brain/text layer stayed at 100% the whole
ramp — so **the bottleneck is the voice-audio path (Gemini Live TTS), not the agent
logic**. Most likely cause: **Gemini Live concurrent-session limits at the provider**
(audio generation throttled/dropped while the faster text transcript still returns).
For a store with many simultaneous voice shoppers, **voice silently degrades to
text-without-speech** — no error, no crash, just no voice.

### 🟠 Finding 2 — Per-turn voice latency is high even at low load
A single voice turn took **~12–18 s** end-to-end even at 10 concurrent. A natural
voice exchange should be ~2–4 s. This is slow enough to hurt the conversational feel
before any scale concerns (note: includes Gemini Live session setup per turn + TTS).

### ✅ Good news
- **No crash, no OOM** at any level — the 3.5 GB box held (app peaked ~455 MB). The
  earlier OOM worries did not materialize for this workload; the limit is voice
  throughput, not memory.
- **Connection layer + brain are robust**: 200/200 sessions connected and got a
  reply at every level. **Text-mode chat would handle 200 concurrent fine.**

## Answer to the question
- **Text assistant at 10×20:** ✅ handles it.
- **Voice assistant at 10×20:** ❌ does not — voice audio collapses above ~50–100
  concurrent. A busy store would see most voice customers get no spoken reply.

## ⚙️ Fixes applied (commit bcf12bd)
- **Voice overflow → text (safety net) — DONE & verified.** Concurrent voice
  sessions are now capped (`VOICE_MAX_CONCURRENT`, default 40 — safely under the
  ~50 collapse point). Past the cap, a customer gets a **text session + a "voice is
  busy — replying by text" notice** instead of silence. **Verified:** cap=5 with 12
  concurrent → all 12 got a reply (5 voice + 7 cleanly shed to text), 0 silent.
- **Latency (#2) — investigated, not yet optimized.** First-turn (cold) voice
  latency measured ~13 s (includes per-connection Gemini Live setup + brain + TTS).
  A true fix = warm/pooled Live sessions, which is a deeper, riskier change; left as
  a scoped follow-up rather than a blind rewrite. Note: the load-test latencies are
  all *cold first-turns* (1 turn/customer), so they overstate steady-state; a real
  multi-turn conversation pays the setup once.

## Recommendations
1. **Treat Gemini Live concurrency as a hard capacity limit.** Measure the provider's
   real concurrent-session quota; pool/queue voice sessions, or cap concurrent voice
   per instance and shed to text gracefully (the text path scales).
2. **Cut per-turn voice latency** (~12–18 s): reuse/warm Gemini Live sessions instead
   of per-turn setup; investigate the TTS streaming path.
3. **Scale horizontally** for voice (more app instances) since one instance saturates
   the provider audio path well before memory.
4. Re-run this ramp after changes; add a real-mic human pass for STT/quality.

## Limits / honesty
- Drove the pipeline with `text_input` (real TTS-out) — **mic→STT not exercised**.
- Single harness IP with the voice rate-limit temporarily raised (reverted after).
- Each customer did one turn; a full multi-turn voice journey would be heavier.

Harness: `test_store/run_voice_load.py` (MODE=setup / MODE=load LEVEL=N).
