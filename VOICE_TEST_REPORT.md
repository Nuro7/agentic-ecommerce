# Speako — Voice Testing Report

Built and ran a **synthetic voice harness** (`test_store/run_voice_test.py`):
generate each utterance as 16 kHz PCM via Google TTS → stream it over the voice WS
`/wooagent/stream` like the browser mic → read back STT transcript + Aria reply +
TTS audio. Goal: exercise the STT → brain → TTS round-trip without a human.

## ⚙️ Fixes applied (commit 108fe8f)
- **Starter voice-gate lockout — FIXED.** The WS required voice unconditionally, so
  Starter/free merchants couldn't chat at all (even text). Now non-voice plans run a
  **text-only** session over the same WS (Pipeline C, 1 credit) instead of being
  rejected. **Verified:** a Starter tenant completes a text turn; a Growth tenant
  still streams TTS audio (no regression).
- **Audio-relay fragility — FIXED.** A per-chunk guard in Pipeline A drops a failed/
  early audio frame instead of tearing down the whole session.

## Key results

### ✅ The voice pipeline works and produces speech
Driving the WS with `text_input` returned a clean stream of **`transcript`** chunks
("…right now. Would you like to see what else is available?") interleaved with
**24 kHz PCM audio** frames, ending in **`turn_complete`**. So Pipeline A
(Gemini Live) establishes, the brain answers, and **TTS audio is generated and
streamed** — the core voice output path is functional.

### ✅ Correction: the earlier "WS drops mid-conversation" (#3) was largely a TEST ARTIFACT
The app runs under `uvicorn --reload` watching the mounted dirs (incl. `static/`).
The journey harness was copied into `static/` to run it — **each copy triggered an
app reload that killed in-flight WebSockets**, which looked like random
mid-conversation drops. Running from `/tmp` (un-watched), the WS stayed stable and
text turns completed cleanly. **Downgrade #3** from a product bug to mostly a
harness/setup artifact (real-audio stability still needs the human pass below).

### ⚠️ Boundary: synthetic raw-mic audio could not drive Gemini Live STT here
Streaming generated PCM as mic frames: after adding a setup delay the connection
stayed open, but **Gemini Live produced no transcription and no response** to the
injected audio (`user_transcript` empty, no audio out). Gemini Live's automatic
VAD/turn detection doesn't fire on injected synthetic PCM the way it does for a
real browser AudioWorklet stream (continuous capture + activity signaling). Fully
automating **audio-in STT** would need either a real browser/mic or implementing
Gemini Live's explicit activity-start/end signaling + exact capture params — deep,
uncertain work beyond this pass.

## What this means
- **Voice OUTPUT (brain → TTS → audio)**: verified working over the WS.
- **Voice INPUT (mic → STT)**: not validatable with synthetic audio here; needs a
  **human voice pass** (or a real browser-driven automation) to confirm STT
  accuracy, accents, languages, noise, barge-in, and latency.

## Recommended voice test plan (the "things needed")
1. **Manual human pass (required):** Chrome + mic + speakers, the widget on a page
   served from a **stable public URL** (Render, not ngrok — it drops WS), a
   **Growth/Pro merchant** (voice-gated), products synced. Speak the journey
   (search → add → cart → checkout → address → "yes"), in English + an Indian
   language, with one barge-in and some background noise. Grade: STT accuracy,
   TTS intelligibility/latency, barge-in, turn-taking, failover.
2. **Optional real-browser automation:** Playwright driving the actual widget with
   a fake-mic WAV (`--use-file-for-fake-audio-capture`) — this uses the real
   AudioWorklet path so Gemini Live's VAD behaves normally; the synthetic
   server-side injection here did not.

## Artifacts
- `test_store/run_voice_test.py` — synthetic voice harness (TTS→PCM→WS→transcripts).
- Verified live: text-driven voice turn returns transcripts + audio + turn_complete.
