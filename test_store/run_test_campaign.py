"""Speako QA test campaign — black-box + in-process checks across multiple merchants.

Runs INSIDE docker-app-1 (so it can import src.app.* and reach the DB/Redis).
  docker exec -w /app docker-app-1 python static/run_test_campaign.py

Test stores must already run on the host (alpha:9001, beta:9002, gamma:9003),
reachable from the container via host.docker.internal.

Writes /app/static/TEST_REPORT.md and prints a JSON summary to stdout.
"""
import sys
sys.path.insert(0, "/app")

import asyncio
import hashlib
import hmac
import json
import os
import time

import httpx
import websockets

# Load ORM models so the shared metadata registry can resolve FKs (this is a
# standalone process, not the app, so nothing is imported for us).
import src.app.modules.tenants.models  # noqa: F401
import src.app.modules.billing.models  # noqa: F401

GROWTH_PLAN_ID = "00000000-0000-0000-0000-000000000002"  # allow_voice: true

BASE = "http://localhost:8000"
API = BASE + "/api/v1"
STORE = "http://host.docker.internal"
RUN = os.getenv("RUN_ID", "r1")
PASSWORD = "test1234"
AGENT_TURN_CAP = 12

MERCHANTS = [
    {"key": "alpha", "name": "Alpha Apparel",     "port": 9001, "apikey": "key-alpha",
     "email": f"alpha+{RUN}@speakotest.com", "ids": {"101", "102", "103", "104", "105"},
     "count": 5, "a_product": "Blue Cotton T-Shirt"},
    {"key": "beta",  "name": "Beta Electronics",  "port": 9002, "apikey": "key-beta",
     "email": f"beta+{RUN}@speakotest.com",  "ids": {"201", "202", "203", "204", "205"},
     "count": 5, "a_product": "Wireless Earbuds Pro"},
    {"key": "gamma", "name": "Gamma Home",        "port": 9003, "apikey": "key-gamma",
     "email": f"gamma+{RUN}@speakotest.com", "ids": {"301", "302", "303", "304"},
     "count": 4, "a_product": "Ceramic Dinner Set"},
]

results = []
_agent_turns = 0


def rec(phase, scenario, status, expected, actual, severity="", evidence=""):
    results.append({
        "phase": phase, "scenario": scenario, "status": status,
        "expected": expected, "actual": str(actual)[:500],
        "severity": severity, "evidence": str(evidence)[:300],
    })
    print(f"[{status:4}] {phase} :: {scenario} -> {str(actual)[:140]}")


async def db_platform_ids(tenant_id):
    from sqlalchemy import text
    from src.app.core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            text("SELECT platform_id FROM product_cache WHERE tenant_id=:t"),
            {"t": tenant_id})).scalars().all()
    return {str(r) for r in rows}


async def run_sync(tenant_id):
    # Await the inner coroutine directly — the Celery task wrapper calls
    # asyncio.run() which can't nest inside our event loop.
    from src.app.workers.tasks.sync_products import _sync_async
    return await _sync_async(tenant_id_filter=tenant_id)


async def upgrade_plan(tenant_id, plan_id):
    from sqlalchemy import text
    from src.app.core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        await db.execute(text("UPDATE subscriptions SET plan_id=:p WHERE tenant_id=:t"),
                         {"p": plan_id, "t": tenant_id})
        await db.commit()


async def seed_credits(tenant_id, n):
    from src.app.core.database import AsyncSessionLocal
    from src.app.modules.billing.service import BillingService
    async with AsyncSessionLocal() as db:
        await BillingService(db).record_usage(tenant_id, "credits", n)
        await db.commit()


async def ws_turn(client_hc, tenant_id, session_id, text_msg, language="en", timeout=30):
    """One agent turn over the voice WS (text_input). Returns (transcript, ui_actions, raw)."""
    global _agent_turns
    if _agent_turns >= AGENT_TURN_CAP:
        return None, [], "SKIPPED (agent turn cap reached)"
    _agent_turns += 1
    url = f"ws://localhost:8000/wooagent/stream?session_id={session_id}&tenant_id={tenant_id}"
    transcript, uia, raws = [], [], []
    try:
        async with websockets.connect(url, open_timeout=15, close_timeout=5, max_size=8_000_000) as ws:
            await ws.send(json.dumps({"type": "text_input", "text": text_msg, "language": language}))
            end = time.time() + timeout
            while time.time() < end:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=end - time.time())
                except asyncio.TimeoutError:
                    break
                if isinstance(msg, (bytes, bytearray)):
                    raws.append(f"<audio {len(msg)}B>")
                    continue
                try:
                    obj = json.loads(msg)
                except Exception:
                    continue
                t = obj.get("type")
                raws.append(t)
                if t == "transcript":
                    transcript.append(obj.get("text", ""))
                elif t == "ui_action":
                    uia.append(obj.get("action"))
                elif t == "turn_complete":
                    break
                elif t == "pipeline_error":
                    transcript.append(f"[pipeline_error: {obj.get('message')}]")
                    break
    except Exception as e:
        return None, [], f"WS error: {type(e).__name__}: {e}"
    return " ".join(transcript).strip(), uia, ",".join(raws)


async def main():
    t0 = time.time()
    hc = httpx.AsyncClient(timeout=30.0)

    # ── Step 1: Onboard 3 merchants ──────────────────────────────────────────
    for m in MERCHANTS:
        body = {
            "store_name": m["name"], "email": m["email"], "password": PASSWORD,
            "platform": "custom_api",
            "custom_api_base_url": f"{STORE}:{m['port']}",
            "custom_api_key": m["apikey"],
        }
        r = await hc.post(f"{API}/onboard/", json=body)
        if r.status_code == 201:
            m["tenant_id"] = r.json()["tenant_id"]
            rec("Onboard", f"create {m['key']}", "PASS", "201 + tenant_id", f"201 {m['tenant_id']}")
        else:
            # maybe already exists from a prior run — look up by api key
            look = await hc.get(f"{API}/onboard/lookup", params={"api_key": m["apikey"]})
            if look.status_code == 200:
                m["tenant_id"] = look.json()["tenant_id"]
                rec("Onboard", f"create {m['key']}", "PASS", "201 or existing", f"{r.status_code}, reused {m['tenant_id']}")
            else:
                rec("Onboard", f"create {m['key']}", "FAIL", "201 + tenant_id", f"{r.status_code} {r.text[:120]}", "high")
                m["tenant_id"] = None

    alpha, beta, gamma = MERCHANTS

    # ── Step 1b: Negative onboarding ─────────────────────────────────────────
    r = await hc.post(f"{API}/onboard/", json={"store_name": "Dup", "email": alpha["email"],
                      "password": PASSWORD, "platform": "custom_api",
                      "custom_api_base_url": f"{STORE}:9001", "custom_api_key": "key-alpha"})
    rec("Onboard-neg", "duplicate email", "PASS" if r.status_code == 409 else "FAIL",
        "409 conflict", r.status_code, "high" if r.status_code != 409 else "")

    r = await hc.post(f"{API}/onboard/", json={"store_name": "NoUrl", "email": f"nourl+{RUN}@speakotest.com",
                      "password": PASSWORD, "platform": "custom_api"})
    rec("Onboard-neg", "missing custom_api_base_url", "PASS" if r.status_code == 422 else "FAIL",
        "422", r.status_code, "med" if r.status_code != 422 else "")

    r = await hc.post(f"{API}/onboard/", json={"store_name": "BadPlat", "email": f"badplat+{RUN}@speakotest.com",
                      "password": PASSWORD, "platform": "magento"})
    rec("Onboard-neg", "invalid platform", "PASS" if r.status_code == 422 else "FAIL",
        "422", r.status_code, "low" if r.status_code != 422 else "")

    r = await hc.post(f"{API}/onboard/test-connection", json={"platform": "custom_api",
                      "custom_api_base_url": f"{STORE}:9001", "custom_api_key": "key-alpha"})
    ok = r.status_code == 200 and r.json().get("ok") and r.json().get("products_found", 0) >= 1
    rec("Onboard", "test-connection reachable", "PASS" if ok else "FAIL", "ok:true, products>=1", r.text[:120])

    r = await hc.post(f"{API}/onboard/test-connection", json={"platform": "custom_api",
                      "custom_api_base_url": f"{STORE}:9999", "custom_api_key": "x"})
    bad = r.status_code == 200 and r.json().get("ok") is False
    rec("Onboard", "test-connection unreachable", "PASS" if bad else "FAIL", "ok:false", r.text[:120])

    # ── Step 2: Sync + per-merchant catalog isolation ────────────────────────
    for m in MERCHANTS:
        if not m.get("tenant_id"):
            continue
        try:
            res = await run_sync(m["tenant_id"])
            ids = await db_platform_ids(m["tenant_id"])
            only_own = ids.issubset(m["ids"])
            full = ids == m["ids"]
            status = "PASS" if (only_own and full) else ("FAIL" if not only_own else "WARN")
            rec("Sync", f"{m['key']} catalog == own {len(m['ids'])}",
                status, f"ids=={sorted(m['ids'])}", f"upserted={res.get('upserted')} cached_ids={sorted(ids)}",
                "high" if not only_own else "")
        except Exception as e:
            rec("Sync", f"{m['key']} sync", "ERROR", "synced", repr(e), "high")

    # cross-tenant product leakage
    try:
        a_ids = await db_platform_ids(alpha["tenant_id"])
        b_ids = await db_platform_ids(beta["tenant_id"])
        leak = a_ids & b_ids
        rec("Isolation", "alpha vs beta product overlap", "PASS" if not leak else "FAIL",
            "no shared platform_ids", f"overlap={sorted(leak)}", "high" if leak else "")
    except Exception as e:
        rec("Isolation", "product overlap", "ERROR", "no overlap", repr(e))

    # ── Step 3: Widget tenant routing isolation (cart) ───────────────────────
    try:
        sess = f"isolation-sess-{RUN}"
        # add to ALPHA store cart for this session
        async with httpx.AsyncClient(timeout=10) as sc:
            await sc.post(f"{STORE}:9001/cart/add",
                          headers={"Authorization": "Bearer key-alpha"},
                          json={"session_id": sess, "product_id": 101, "quantity": 1})
        ra = await hc.get(f"{API}/cart", params={"session_id": sess},
                          headers={"X-Tenant-ID": alpha["tenant_id"]})
        rb = await hc.get(f"{API}/cart", params={"session_id": sess},
                          headers={"X-Tenant-ID": beta["tenant_id"]})
        a_items = ra.json().get("item_count", 0) if ra.status_code == 200 else -1
        b_items = rb.json().get("item_count", 0) if rb.status_code == 200 else -1
        ok = a_items >= 1 and b_items == 0
        rec("Isolation", "cart routing alpha!=beta", "PASS" if ok else "FAIL",
            "alpha cart has item, beta empty", f"alpha={a_items} beta={b_items}",
            "high" if not ok else "")
    except Exception as e:
        rec("Isolation", "cart routing", "ERROR", "isolated", repr(e))

    # ── Step 3b: JWT login + IDOR ────────────────────────────────────────────
    tokens = {}
    for m in (alpha, beta):
        r = await hc.post(f"{API}/auth/login", json={"email": m["email"], "password": PASSWORD})
        if r.status_code == 200 and r.json().get("access_token"):
            tokens[m["key"]] = r.json()["access_token"]
            rec("Auth", f"login {m['key']}", "PASS", "200 + token", "200")
        else:
            rec("Auth", f"login {m['key']}", "FAIL", "200 + token", f"{r.status_code} {r.text[:100]}", "high")
    if "alpha" in tokens:
        h = {"Authorization": f"Bearer {tokens['alpha']}"}
        r = await hc.get(f"{API}/tenants/me", headers=h)
        rec("Auth", "alpha /tenants/me", "PASS" if r.status_code == 200 else "FAIL", "200", r.status_code)
        if beta.get("tenant_id"):
            r = await hc.get(f"{API}/tenants/{beta['tenant_id']}", headers=h)
            ok = r.status_code in (403, 404)
            rec("Isolation", "IDOR alpha->beta tenant", "PASS" if ok else "FAIL",
                "403/404", r.status_code, "critical" if not ok else "")

    # ── Step 4: Quota (seed 50 then expect 402) ──────────────────────────────
    qbody = {"store_name": "Quota Co", "email": f"quota+{RUN}@speakotest.com", "password": PASSWORD,
             "platform": "custom_api", "custom_api_base_url": f"{STORE}:9001", "custom_api_key": f"key-quota-{RUN}"}
    rq = await hc.post(f"{API}/onboard/", json=qbody)
    if rq.status_code == 201:
        qid = rq.json()["tenant_id"]
        try:
            # Starter plan limit is 200; seed just over it then expect 402.
            await seed_credits(qid, 205)
            r = await hc.post(f"{API}/greet", json={"session_id": f"q-{RUN}", "language": "en"},
                              headers={"X-Tenant-ID": qid})
            ok = r.status_code == 402
            rec("Quota", "greet after 205/200 credits", "PASS" if ok else "FAIL",
                "402 payment required", r.status_code, "high" if not ok else "")
        except Exception as e:
            rec("Quota", "seed+enforce", "ERROR", "402", repr(e), "med")
    else:
        rec("Quota", "quota merchant onboard", "ERROR", "201", rq.status_code)

    # ── Step 4b: Rate limit (cart 60/min) ────────────────────────────────────
    try:
        codes = []
        for i in range(70):
            r = await hc.get(f"{API}/cart", params={"session_id": f"rl-{RUN}"},
                             headers={"X-Tenant-ID": alpha["tenant_id"]})
            codes.append(r.status_code)
        n429 = codes.count(429)
        rec("RateLimit", "70x /cart (limit 60/min)", "PASS" if n429 > 0 else "WARN",
            "some 429 after 60", f"429count={n429} last={codes[-1]}", "med" if n429 == 0 else "")
    except Exception as e:
        rec("RateLimit", "burst cart", "ERROR", "429 seen", repr(e))

    # ── Step 5: Webhooks (custom, HMAC) ──────────────────────────────────────
    try:
        from src.app.config import settings
        secret = settings.shared_secret.encode()
        payload = json.dumps([{"id": 101, "name": "Blue Cotton T-Shirt", "price": 19.99}]).encode()
        good = hmac.new(secret, payload, hashlib.sha256).hexdigest()
        wid = alpha["tenant_id"]
        r = await hc.post(f"{API}/webhooks/custom/{wid}", content=payload,
                          headers={"X-Speako-Signature": good, "X-Speako-Topic": "product.updated",
                                   "Content-Type": "application/json"})
        rec("Webhook", "custom valid HMAC", "PASS" if r.status_code < 300 else "FAIL",
            "2xx", f"{r.status_code} {r.text[:80]}", "med" if r.status_code >= 300 else "")
        r = await hc.post(f"{API}/webhooks/custom/{wid}", content=payload,
                          headers={"X-Speako-Signature": "deadbeef", "X-Speako-Topic": "product.updated",
                                   "Content-Type": "application/json"})
        rec("Webhook", "custom bad HMAC", "PASS" if r.status_code == 401 else "FAIL",
            "401", r.status_code, "high" if r.status_code != 401 else "")
        r = await hc.post(f"{API}/webhooks/custom/{wid}", content=payload,
                          headers={"X-Speako-Topic": "product.updated", "Content-Type": "application/json"})
        rec("Webhook", "custom missing HMAC", "PASS" if r.status_code == 401 else "FAIL",
            "401", r.status_code, "high" if r.status_code != 401 else "")
    except Exception as e:
        rec("Webhook", "custom hmac", "ERROR", "signed ok / unsigned 401", repr(e))

    # ── Step 4c: Edge cases ──────────────────────────────────────────────────
    r = await hc.post(f"{API}/onboard/", content=b"{not json", headers={"Content-Type": "application/json"})
    rec("Edge", "malformed JSON onboard", "PASS" if r.status_code in (400, 422) else "FAIL",
        "400/422", r.status_code)

    # voice WS: missing session_id must be rejected (fix validation)
    try:
        async with websockets.connect("ws://localhost:8000/wooagent/stream", open_timeout=10) as ws:
            try:
                await asyncio.wait_for(ws.recv(), timeout=3)
            except Exception:
                pass
        rec("Edge", "voice WS missing session_id", "FAIL", "connection rejected (4003)", "connection stayed open", "high")
    except Exception as e:
        rec("Edge", "voice WS missing session_id", "PASS", "rejected (4003)", f"closed: {type(e).__name__}")

    # voice WS: short session_id rejected
    try:
        async with websockets.connect("ws://localhost:8000/wooagent/stream?session_id=abc", open_timeout=10) as ws:
            try:
                await asyncio.wait_for(ws.recv(), timeout=3)
            except Exception:
                pass
        rec("Edge", "voice WS short session_id", "FAIL", "rejected (4003)", "stayed open", "high")
    except Exception as e:
        rec("Edge", "voice WS short session_id", "PASS", "rejected (4003)", f"closed: {type(e).__name__}")

    # concurrent greets same session (session-lock fix smoke test)
    try:
        sid = f"concurrent-{RUN}"
        rs = await asyncio.gather(*[
            hc.post(f"{API}/greet", json={"session_id": sid, "language": "en"},
                    headers={"X-Tenant-ID": alpha["tenant_id"]})
            for _ in range(5)], return_exceptions=True)
        codes = [getattr(x, "status_code", repr(x)) for x in rs]
        n5xx = sum(1 for c in codes if isinstance(c, int) and c >= 500)
        rec("Edge", "5 concurrent greets same session", "PASS" if n5xx == 0 else "FAIL",
            "no 5xx", f"codes={codes}", "high" if n5xx else "")
    except Exception as e:
        rec("Edge", "concurrent greets", "ERROR", "no 5xx", repr(e))

    # ── Step 5b: Voice gate — starter plan must be blocked on the WS ─────────
    try:
        transcript, uia, raw = await ws_turn(hc, gamma["tenant_id"], f"gate-{RUN}", "hello", "en", timeout=15)
        blocked = transcript is not None and "voice" in (transcript or "").lower() and "not available" in (transcript or "").lower()
        rec("VoiceGate", "starter plan WS blocked", "PASS" if blocked else "WARN",
            "starter rejected with voice-gate msg", (transcript or raw)[:120],
            "high" if not blocked else "")
    except Exception as e:
        rec("VoiceGate", "starter WS", "ERROR", "blocked", repr(e))

    # ── Step 6: Agent conversations (SAMPLED, capped) via WS ──────────────────
    # Upgrade alpha+beta to growth (allow_voice) so the WS assistant is reachable.
    for m in (alpha, beta):
        try:
            await upgrade_plan(m["tenant_id"], GROWTH_PLAN_ID)
        except Exception as e:
            rec("Agent", f"upgrade {m['key']} to growth", "ERROR", "voice enabled", repr(e), "high")
    # Alpha (apparel): search, add-to-cart, off-topic, hallucination, multilingual
    a_id, a_sess = alpha["tenant_id"], f"agent-alpha-{RUN}"
    convs = [
        (alpha, a_sess, "Show me your products", "en", "lists alpha products"),
        (alpha, a_sess, "Add the blue cotton t-shirt to my cart", "en", "ui_action add_to_cart"),
        (alpha, a_sess, "What's in my cart?", "en", "mentions the shirt"),
        (alpha, a_sess, "What's the weather today?", "en", "refuses off-topic"),
        (alpha, a_sess, "Do you sell a purple dragon costume in size XXL?", "en", "no fabrication"),
        (alpha, a_sess, "mujhe kuch sasta dikhao", "hi", "responds (Hindi)"),
        (beta, f"agent-beta-{RUN}", "What do you sell?", "en", "lists beta electronics"),
        (beta, f"agent-beta-{RUN}", "Show me earbuds", "en", "finds Wireless Earbuds Pro"),
    ]
    beta_names = {"earbud", "keyboard", "webcam", "charger", "watch"}
    for m, sess, msg, lang, expect in convs:
        transcript, uia, raw = await ws_turn(hc, m["tenant_id"], sess, msg, lang)
        if transcript is None:
            rec("Agent", f"{m['key']}: {msg[:34]}", "ERROR", expect, raw, "high")
            continue
        low = transcript.lower()
        status, sev, note = "PASS", "", expect
        if "add the blue" in msg.lower():
            added = any((a or {}).get("type", "").startswith("add") for a in uia)
            status = "PASS" if added else "WARN"
            note = f"ui_actions={[ (a or {}).get('type') for a in uia]}"
            sev = "med" if not added else ""
        elif "weather" in msg.lower():
            refused = ("weather" not in low) or ("shop" in low or "store" in low or "help you" in low)
            status = "PASS" if refused else "FAIL"
            sev = "med" if not refused else ""
            note = "off-topic handling"
        elif "dragon" in msg.lower():
            fabricated = "dragon" in low and ("yes" in low or "$" in transcript or "add" in low)
            status = "FAIL" if fabricated else "PASS"
            sev = "high" if fabricated else ""
            note = "hallucination guard"
        elif m["key"] == "alpha" and "products" in msg.lower():
            leaks_beta = any(b in low for b in beta_names)
            status = "FAIL" if leaks_beta else "PASS"
            sev = "high" if leaks_beta else ""
            note = "alpha shows own catalog, no beta leak"
        rec("Agent", f"{m['key']}: {msg[:34]}", status, expect,
            (transcript[:160] or "<empty>"), sev, f"{note}; raw={raw}")

    await hc.aclose()

    # ── Report ───────────────────────────────────────────────────────────────
    dur = time.time() - t0
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    summary = {"duration_s": round(dur, 1), "agent_turns": _agent_turns,
               "counts": counts, "results": results}

    lines = ["# Speako — Automated Test Campaign Report", ""]
    lines.append(f"Run `{RUN}` · {len(results)} checks · agent turns used: {_agent_turns}/{AGENT_TURN_CAP} · {round(dur,1)}s")
    lines.append("")
    lines.append("| Status | Count |")
    lines.append("|---|---|")
    for k in ("PASS", "WARN", "FAIL", "ERROR"):
        if counts.get(k):
            lines.append(f"| {k} | {counts[k]} |")
    lines.append("")
    lines.append("| # | Phase | Scenario | Status | Expected | Actual | Sev |")
    lines.append("|---|---|---|---|---|---|---|")
    for i, r in enumerate(results, 1):
        a = r["actual"].replace("|", "\\|").replace("\n", " ")[:90]
        e = r["expected"].replace("|", "\\|")[:50]
        lines.append(f"| {i} | {r['phase']} | {r['scenario']} | **{r['status']}** | {e} | {a} | {r['severity']} |")
    lines.append("")
    fails = [r for r in results if r["status"] in ("FAIL", "ERROR")]
    if fails:
        lines.append("## Failures & errors (detail)")
        for r in fails:
            lines.append(f"- **[{r['status']}] {r['phase']} :: {r['scenario']}** (sev={r['severity']})")
            lines.append(f"  - expected: {r['expected']}")
            lines.append(f"  - actual: {r['actual']}")
            if r["evidence"]:
                lines.append(f"  - evidence: {r['evidence']}")
    report = "\n".join(lines)
    with open("/app/static/TEST_REPORT.md", "w", encoding="utf-8") as f:
        f.write(report)
    print("\n=== JSON_SUMMARY ===")
    print(json.dumps(summary))


if __name__ == "__main__":
    asyncio.run(main())
