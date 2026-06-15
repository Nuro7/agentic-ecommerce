"""Speako — VOICE concurrency load test (text-in -> real TTS-out over the voice WS).

Drives the voice pipeline (Gemini Live session + brain + TTS audio) at concurrency.
Does NOT test mic STT (can't be automated) — each "customer" sends one text_input
and we measure: connection ok, TTS audio produced, latency, errors.

  MODE=setup                      -> onboard 10 merchants, upgrade to growth, sync
  MODE=load LEVEL=25              -> fire 25 concurrent voice sessions across them

Run from /tmp (NOT static — static is hot-reload-watched and would kill WSs).
"""
import sys
sys.path.insert(0, "/app")

import asyncio
import json
import os
import statistics as st
import time

import httpx
import websockets
import src.app.modules.tenants.models   # noqa: F401
import src.app.modules.billing.models    # noqa: F401

API = "http://localhost:8000/api/v1"
WS = "ws://localhost:8000/wooagent/stream"
STORE = "http://host.docker.internal:9100"
GROWTH = "00000000-0000-0000-0000-000000000002"
PASSWORD = "test1234"
N_MERCHANTS = 10
MODE = os.getenv("MODE", "load")
LEVEL = int(os.getenv("LEVEL", "10"))


async def setup():
    from sqlalchemy import text
    from src.app.core.database import AsyncSessionLocal
    from src.app.workers.tasks.sync_products import _sync_async
    hc = httpx.AsyncClient(timeout=60.0)
    tids = []
    for i in range(N_MERCHANTS):
        body = {"store_name": f"VoiceLoad M{i}", "email": f"voiceload-{i}@speakotest.com",
                "password": PASSWORD, "platform": "custom_api",
                "custom_api_base_url": STORE, "custom_api_key": f"key-m-{i}"}
        r = await hc.post(f"{API}/onboard/", json=body)
        if r.status_code == 201:
            tids.append(r.json()["tenant_id"])
        else:
            lk = await hc.get(f"{API}/onboard/lookup", params={"api_key": f"key-m-{i}"})
            if lk.status_code == 200:
                tids.append(lk.json()["tenant_id"])
    # upgrade all to growth (voice) + sync
    async with AsyncSessionLocal() as db:
        for t in tids:
            await db.execute(text("UPDATE subscriptions SET plan_id=:p WHERE tenant_id=:t"), {"p": GROWTH, "t": t})
        await db.commit()
    synced = 0
    for t in tids:
        try:
            await _sync_async(tenant_id_filter=t); synced += 1
        except Exception as e:
            print("sync err", t, e)
    await hc.aclose()
    print(json.dumps({"merchants": len(tids), "synced": synced, "tids": tids}))


async def voice_session(tid, n, utter="show me shirts"):
    sid = f"vl-{tid[:8]}-{n}-{int(LEVEL)}"
    t0 = time.time()
    audio, got_reply, types = 0, False, []
    try:
        async with websockets.connect(f"{WS}?session_id={sid}&tenant_id={tid}",
                                      open_timeout=20, max_size=16_000_000) as ws:
            await ws.send(json.dumps({"type": "text_input", "text": utter, "language": "en"}))
            end = time.time() + 45
            while time.time() < end:
                try:
                    m = await asyncio.wait_for(ws.recv(), timeout=end - time.time())
                except asyncio.TimeoutError:
                    break
                if isinstance(m, (bytes, bytearray)):
                    audio += len(m); got_reply = True; continue
                try:
                    o = json.loads(m)
                except Exception:
                    continue
                ty = o.get("type")
                if ty in ("transcript", "ui_action"):
                    got_reply = True
                if ty == "turn_complete":
                    types.append("done"); break
                if ty == "pipeline_error":
                    types.append("perr"); break
        ok = got_reply
        return {"ok": ok, "audio": audio, "ms": (time.time() - t0) * 1000, "err": None}
    except Exception as e:
        return {"ok": False, "audio": 0, "ms": (time.time() - t0) * 1000, "err": f"{type(e).__name__}"}


async def load():
    from sqlalchemy import text
    from src.app.core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        tids = [str(r) for r in (await db.execute(
            text("SELECT id FROM tenants WHERE email LIKE 'voiceload-%' ORDER BY email"))).scalars().all()]
    if not tids:
        print(json.dumps({"error": "no voiceload merchants — run MODE=setup first"})); return
    t0 = time.time()
    res = await asyncio.gather(*[voice_session(tids[i % len(tids)], i) for i in range(LEVEL)],
                               return_exceptions=True)
    res = [r for r in res if isinstance(r, dict)]
    ok = sum(1 for r in res if r["ok"])
    lat = [r["ms"] for r in res if r["ok"]]
    errs = {}
    for r in res:
        if r["err"]:
            errs[r["err"]] = errs.get(r["err"], 0) + 1
    audio_ok = sum(1 for r in res if r["audio"] > 0)
    out = {
        "level": LEVEL, "ok": ok, "fail": LEVEL - ok, "audio_sessions": audio_ok,
        "ok_pct": round(100 * ok / LEVEL, 1),
        "p50_ms": round(st.median(lat)) if lat else 0,
        "p95_ms": round(sorted(lat)[int(len(lat) * 0.95) - 1]) if len(lat) > 5 else (max(lat) if lat else 0),
        "max_ms": round(max(lat)) if lat else 0,
        "wall_s": round(time.time() - t0, 1),
        "errors": errs,
    }
    print(json.dumps(out))


if __name__ == "__main__":
    asyncio.run(setup() if MODE == "setup" else load())
