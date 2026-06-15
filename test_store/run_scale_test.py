"""Speako SCALE test — N merchants x M products each. Runs inside docker-app-1.

  docker exec -e RUN_ID=<x> -e N_MERCHANTS=40 docker-app-1 python static/run_scale_test.py

Requires the scale store running on the host (port 9100), reachable via
host.docker.internal. Writes /app/static/SCALE_REPORT.md + JSON to stdout.
"""
import sys
sys.path.insert(0, "/app")

import asyncio
import json
import os
import time

import httpx
import src.app.modules.tenants.models  # noqa: F401  (load ORM metadata)
import src.app.modules.billing.models  # noqa: F401

API = "http://localhost:8000/api/v1"
STORE = "http://host.docker.internal:9100"
RUN = os.getenv("RUN_ID", "s1")
N = int(os.getenv("N_MERCHANTS", "40"))
PER = int(os.getenv("PRODUCTS_PER", "1000"))
PASSWORD = "test1234"

log = lambda m: print(f"[{round(time.time()%100000,1)}] {m}", flush=True)


async def db_count(tenant_id):
    from sqlalchemy import text
    from src.app.core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        return (await db.execute(
            text("SELECT count(*) FROM product_cache WHERE tenant_id=:t"), {"t": tenant_id})).scalar()


async def db_ids_sample(tenant_id, lim=5):
    from sqlalchemy import text
    from src.app.core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        return [str(r) for r in (await db.execute(
            text("SELECT platform_id FROM product_cache WHERE tenant_id=:t LIMIT :l"),
            {"t": tenant_id, "l": lim})).scalars().all()]


async def main():
    findings = []
    rec = lambda *a: (findings.append(a), log(f"{a[0]} | {a[1]} -> {a[2]}"))
    hc = httpx.AsyncClient(timeout=60.0)

    # ── 1. Onboard N merchants ───────────────────────────────────────────────
    t_on = time.time()
    merchants = []
    onboard_fail = 0
    for i in range(N):
        body = {"store_name": f"Scale M{i}", "email": f"scale-{i}+{RUN}@speakotest.com",
                "password": PASSWORD, "platform": "custom_api",
                "custom_api_base_url": STORE, "custom_api_key": f"key-m-{i}"}
        try:
            r = await hc.post(f"{API}/onboard/", json=body)
            if r.status_code == 201:
                merchants.append({"i": i, "tid": r.json()["tenant_id"]})
            else:
                look = await hc.get(f"{API}/onboard/lookup", params={"api_key": f"key-m-{i}"})
                if look.status_code == 200:
                    merchants.append({"i": i, "tid": look.json()["tenant_id"]})
                else:
                    onboard_fail += 1
        except Exception as e:
            onboard_fail += 1
            log(f"onboard {i} err: {e}")
    on_dur = time.time() - t_on
    rec("Onboard", f"{N} merchants ({onboard_fail} failed)", f"{len(merchants)} ok in {on_dur:.1f}s")

    # ── 2. Full sync (timed) ─────────────────────────────────────────────────
    from src.app.workers.tasks.sync_products import _sync_async
    t_sync = time.time()
    sync_err = None
    try:
        res = await _sync_async()
    except Exception as e:
        sync_err = f"{type(e).__name__}: {e}"
        res = {}
    sync_dur = time.time() - t_sync
    if sync_err:
        rec("Sync", "full _sync_async", f"ERROR after {sync_dur:.1f}s: {sync_err}")
    else:
        rec("Sync", "full _sync_async", f"{res} in {sync_dur:.1f}s")

    # ── 3. Per-merchant counts + isolation ───────────────────────────────────
    counts = []
    wrong_range = 0
    short = 0
    for m in merchants:
        c = await db_count(m["tid"])
        counts.append(c)
        if c < PER:
            short += 1
        # isolation: sample ids must fall in [i*1e6+1 .. +PER]
        ids = await db_ids_sample(m["tid"])
        lo, hi = m["i"] * 1_000_000, m["i"] * 1_000_000 + PER
        if any(not (lo < int(x) <= hi) for x in ids if x.isdigit()):
            wrong_range += 1
    total = sum(counts)
    rec("Catalog", f"total products cached", f"{total} (expected {len(merchants)*PER})")
    rec("Catalog", "merchants below target count", f"{short}/{len(merchants)} short (min={min(counts) if counts else 0} max={max(counts) if counts else 0})")
    rec("Isolation", "merchants with out-of-range product ids", f"{wrong_range}/{len(merchants)}")

    # cross-tenant overlap on a sample pair
    if len(merchants) >= 2:
        a = set(await db_ids_sample(merchants[0]["tid"], 50))
        b = set(await db_ids_sample(merchants[-1]["tid"], 50))
        rec("Isolation", "sample overlap M0 vs Mlast", f"{len(a & b)} shared (want 0)")

    # ── 4. Search latency at scale ───────────────────────────────────────────
    try:
        from src.app.agent.retrieval.search import hybrid_search
        from src.app.core.database import AsyncSessionLocal
        lat = []
        async with AsyncSessionLocal() as db:
            for m in merchants[:5]:
                t = time.time()
                rs = await hybrid_search(m["tid"], "shirt", db=db, redis=None, limit=5)
                lat.append((time.time() - t) * 1000)
        rec("Search", "hybrid_search latency (5 tenants, cold)",
            f"avg={sum(lat)/len(lat):.0f}ms max={max(lat):.0f}ms n_results_ok={len(rs)}")
    except Exception as e:
        rec("Search", "hybrid_search", f"ERROR: {type(e).__name__}: {e}")

    await hc.aclose()

    # ── Report ───────────────────────────────────────────────────────────────
    lines = ["# Speako — Scale Test Report", "",
             f"Run `{RUN}` · target {N} merchants × {PER} products = {N*PER} total", ""]
    lines += ["| Phase | Check | Result |", "|---|---|---|"]
    for ph, ck, rs in findings:
        lines.append(f"| {ph} | {ck} | {str(rs).replace('|','/')} |")
    lines += ["", f"- Onboard time: {on_dur:.1f}s",
              f"- Sync time: {sync_dur:.1f}s" + (f"  (ERROR: {sync_err})" if sync_err else ""),
              f"- Total cached: {total} / {len(merchants)*PER}"]
    with open("/app/static/SCALE_REPORT.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("=== JSON ===")
    print(json.dumps({"merchants": len(merchants), "total_products": total,
                      "onboard_s": round(on_dur, 1), "sync_s": round(sync_dur, 1),
                      "sync_err": sync_err, "short": short, "wrong_range": wrong_range,
                      "min": min(counts) if counts else 0, "max": max(counts) if counts else 0}))


if __name__ == "__main__":
    asyncio.run(main())
