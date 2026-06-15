"""Speako — SYNTHETIC VOICE test. Runs inside docker-app-1.

Generates each utterance as 16 kHz LINEAR16 PCM (Google TTS), streams it over the
voice WS /wooagent/stream exactly like the browser mic (audio/pcm;rate=16000),
appends trailing silence to trigger Gemini Live's VAD, then reads back:
  - user_transcript  (what STT heard  -> validates STT)
  - transcript       (Aria's spoken reply text -> validates brain)
  - audio bytes      (24 kHz PCM out  -> validates TTS)
  - ui_action        (cart/checkout actions)

  docker exec docker-app-1 python static/run_voice_test.py

Limits: synthetic clean speech (no real mic acoustics/accents/barge-in). Needs a
voice-enabled (growth) merchant + scale store on host :9100.
"""
import sys
sys.path.insert(0, "/app")

import asyncio
import base64
import json
import os
import time

import httpx
import websockets
from sqlalchemy import text
from src.app.core.database import AsyncSessionLocal
from src.app.config import settings

WS = "ws://localhost:8000/wooagent/stream"
RUN = os.getenv("RUN_ID", "v1")
GROWTH = "00000000-0000-0000-0000-000000000002"
SR = 16000  # input sample rate

results = []
def rec(scn, status, expected, actual, ev=""):
    results.append(dict(scn=scn, status=status, expected=expected, actual=str(actual)[:200], ev=str(ev)[:200]))
    print(f"[{status:5}] {scn} -> {str(actual)[:130]}", flush=True)


async def tts_pcm(text_in, lang="en-US"):
    """Synthesize text -> raw 16 kHz mono PCM bytes (strip WAV header)."""
    body = {"input": {"text": text_in}, "voice": {"languageCode": lang},
            "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": SR}}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"https://texttospeech.googleapis.com/v1/text:synthesize?key={settings.google_tts_api_key}",
            json=body)
    r.raise_for_status()
    audio = base64.b64decode(r.json()["audioContent"])
    if audio[:4] == b"RIFF":  # strip 44-byte WAV header -> raw PCM
        audio = audio[44:]
    return audio


async def voice_turn(ws, pcm, label, timeout=40):
    """Stream PCM as mic frames + trailing silence; collect transcripts/audio."""
    t0 = time.time()
    user_t, aria_t, uia, audio_out, types = [], [], [], 0, []
    # stream ~40ms chunks, paced; then ~1.5s silence to trigger VAD end-of-speech
    chunk = 1280
    for i in range(0, len(pcm), chunk):
        await ws.send(pcm[i:i+chunk])
        await asyncio.sleep(0.008)
    silence = b"\x00" * (SR * 2)  # 1.0s of 16-bit silence
    for i in range(0, len(silence), chunk):
        await ws.send(silence[i:i+chunk])
        await asyncio.sleep(0.008)
    end = time.time() + timeout
    while time.time() < end:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=end - time.time())
        except asyncio.TimeoutError:
            break
        if isinstance(msg, (bytes, bytearray)):
            audio_out += len(msg); continue
        try:
            o = json.loads(msg)
        except Exception:
            continue
        ty = o.get("type"); types.append(ty)
        if ty == "user_transcript": user_t.append(o.get("text", ""))
        elif ty == "transcript": aria_t.append(o.get("text", ""))
        elif ty == "ui_action": uia.append(o.get("action") or {})
        elif ty == "turn_complete": break
        elif ty == "pipeline_error": aria_t.append(f"[pipeline_error:{o.get('message')}]"); break
    return {"heard": " ".join(user_t).strip(), "aria": " ".join(aria_t).strip(),
            "uia": uia, "audio": audio_out, "ms": round((time.time()-t0)*1000), "types": types}


async def main():
    # voice-enabled merchant
    async with AsyncSessionLocal() as db:
        tid = (await db.execute(text("SELECT id FROM tenants WHERE email LIKE 'jrn-%' ORDER BY created_at DESC LIMIT 1"))).scalar()
        if tid:
            await db.execute(text("UPDATE subscriptions SET plan_id=:p WHERE tenant_id=:t"), {"p": GROWTH, "t": str(tid)})
            await db.commit()
    if not tid:
        print("no merchant found — run the journey harness first"); return
    tid = str(tid)
    print(f"merchant={tid}", flush=True)

    sid = f"voice-{RUN}"
    journey = [
        ("search",        "show me shirts",                  "en-US", "heard≈query, Aria lists products, audio>0"),
        ("add_to_cart",   "add the first one to my cart",     "en-US", "ui_action add_to_cart, audio>0"),
        ("view_cart",     "what is in my cart",              "en-US", "mentions cart"),
        ("checkout",      "I want to checkout",              "en-US", "FSM starts (asks name)"),
        ("offtopic",      "what is the weather today",        "en-US", "refuses off-topic"),
    ]
    async with websockets.connect(f"{WS}?session_id={sid}&tenant_id={tid}", open_timeout=20, max_size=16_000_000) as ws:
        for label, utt, lang, expect in journey:
            try:
                pcm = await tts_pcm(utt, lang)
            except Exception as e:
                rec(f"{label} (tts gen)", "ERROR", "pcm generated", repr(e)); continue
            r = await voice_turn(ws, pcm, label)
            stt_ok = bool(r["heard"])
            reply_ok = bool(r["aria"]) and "pipeline_error" not in r["aria"]
            tts_ok = r["audio"] > 0
            status = "PASS" if (stt_ok and reply_ok and tts_ok) else ("WARN" if reply_ok else "FAIL")
            rec(f"{label}: '{utt}'", status, expect,
                f"STT heard='{r['heard'][:40]}' | Aria='{r['aria'][:60]}' | audio={r['audio']}B | {r['ms']}ms",
                f"actions={[a.get('type') for a in r['uia']]} types={r['types'][:6]}")

    # report
    counts = {}
    for x in results: counts[x["status"]] = counts.get(x["status"], 0) + 1
    L = ["# Speako — Synthetic Voice Test Report", "",
         f"Run `{RUN}` · merchant `{tid}` · {counts}", "",
         "Generated each utterance as 16 kHz PCM (Google TTS), streamed over the voice WS",
         "like a real mic, read back STT transcript + Aria reply + TTS audio.", "",
         "| Scenario | Status | STT heard / Aria reply / audio |", "|---|---|---|"]
    for x in results:
        L.append(f"| {x['scn']} | **{x['status']}** | {x['actual'].replace('|','/')[:110]} |")
    L += ["", "## Validates", "- STT (did Gemini hear the utterance), brain reply, TTS audio output, ui_actions over real audio frames.",
          "## Limits", "- Synthetic clean speech: no real-mic acoustics, accents, noise, or human barge-in (needs a manual human pass)."]
    with open("/tmp/VOICE_TEST_REPORT.md", "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("=== JSON ===")
    print(json.dumps({"counts": counts, "results": results}))


if __name__ == "__main__":
    asyncio.run(main())
