"""B2B Blinkit + Swiggy inventory — deployable variant (NO Zepto, NO login).

Self-contained: search + store map run server-side from a bundled 45 MB
`data/catalog_lite.db` (124k Blinkit+Swiggy products, FTS5) + store CSVs, so the
whole thing fits GitHub with no big-DB hosting hack.

Live inventory (the remaining wiring):
  - Swiggy  -> server-side via instamart.in (works from a datacenter IP)
  - Blinkit -> fetched CLIENT-SIDE in the visitor's browser (open CORS, their IP)
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import dark_stores as ds
# Imported at module scope, NOT lazily inside the request handler: a broken curl_cffi wheel
# must fail the DEPLOY, not surface as a raw 500 to the first visitor who clicks a Swiggy check.
import swiggy_probe

HERE = Path(__file__).resolve().parent
DB = HERE / "data" / "catalog_lite.db"
FRONTEND = HERE.parent / "frontend"
PLATS = ("blinkit", "swiggy")   # Zepto dropped

app = FastAPI(title="B2B Blinkit + Swiggy inventory")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---- abuse guards. The link is public with no auth, and every uncached Swiggy check fans
# out ~25 outbound calls to instamart.in from ONE egress IP -- an organic Reddit spike is
# what gets that IP blocked, so both a per-IP limit and a global valve are needed. ----
_HITS: dict = {}
_HITS_LOCK = threading.Lock()
_SW_GATE = threading.Semaphore(4)     # max concurrent instamart fan-outs
_SW_LOCK = threading.Lock()           # collapses a thundering herd onto one probe


@app.middleware("http")
async def ratelimit(request: Request, call_next):
    if request.url.path.startswith("/api/b2b/"):
        # Behind Fly's proxy request.client.host is the PROXY (uvicorn trusts only
        # 127.0.0.1 by default), so keying on it would rate-limit all visitors as one.
        ip = request.headers.get("Fly-Client-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0].strip() \
            or (request.client.host if request.client else "?")
        now = time.time()
        with _HITS_LOCK:
            q = [t for t in _HITS.get(ip, []) if now - t < 60]
            q.append(now)
            _HITS[ip] = q
            if len(_HITS) > 5000:
                _HITS.clear()
        if len(q) > 40:
            return JSONResponse({"detail": "Too many requests — slow down a moment."},
                                status_code=429)
    return await call_next(request)

# zone-price cache (prices ~city-uniform): one live Swiggy call per (query,~11km cell)
_SW_CACHE: dict = {}
_SW_TTL = 60 * 60        # 15m -> 60m: the single biggest lever against Swiggy throttling
_SW_STALE_MAX = 12 * 3600  # beyond fresh, still serve a past result (labelled) rather than nothing


def _db():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


@app.get("/api/health")
def health():
    return {"ok": True, "stores": ds.counts(), "cities": len(ds.all_cities())}


@app.get("/api/b2b/cities")
def cities():
    per = {c: {p: len(ds.stores_in_city(p, c)) for p in PLATS} for c in ds.all_cities()}
    ordered = sorted(per, key=lambda c: -sum(per[c].values()))
    return {"cities": ordered, "store_totals": ds.counts(), "by_city": per}


@app.get("/api/b2b/localities")
def localities(city: str = Query(...)):
    return {"city": city, "localities": [
        {"name": l, "stores": {p: len(ds.stores_in_city(p, city, l)) for p in PLATS}}
        for l in ds.localities(city)]}


@app.get("/api/b2b/stores")
def stores(city: str = Query("Bengaluru")):
    out = {}
    for p in PLATS:
        out[p] = [{"id": s.store_id, "name": s.name, "lat": s.lat, "lng": s.lng, "locality": s.locality}
                  for s in ds.stores_in_city(p, city)]
    pts = [(s["lat"], s["lng"]) for v in out.values() for s in v]
    center = ({"lat": sum(p[0] for p in pts) / len(pts), "lng": sum(p[1] for p in pts) / len(pts)}
              if pts else {"lat": 12.9716, "lng": 77.5946})
    return {"center": center, "counts": {p: len(out.get(p, [])) for p in PLATS}, **out}


@app.get("/api/b2b/catalog-search")
def catalog_search(q: str = Query(..., min_length=1, max_length=64),
                   platform: str | None = None,
                   limit: int = Query(24, ge=1, le=50)):
    # All three bounds are load-bearing on a 512 MB box, measured against this DB:
    # unbounded `limit` served 11.5 MB from ONE request (limit=-1 -> no LIMIT applied),
    # and a 3000-token q pinned a core for 24 s -- both from a single unauthenticated GET.
    con = _db()
    match = " ".join(t + "*" for t in q.split()[:6] if t)
    where, params = "search MATCH ?", [match]
    if platform in PLATS:
        where += " AND p.platform = ?"
        params.append(platform)
    try:
        rows = con.execute(
            f"SELECT p.platform,p.product_id,p.parent_id,p.spin_id,p.name,p.brand,p.pack_size,p.mrp,p.image_url "
            f"FROM search JOIN products p ON p.rowid = search.rowid WHERE {where} "
            f"ORDER BY length(p.name) LIMIT ?", params + [limit]).fetchall()
    except sqlite3.Error:
        rows = []
    con.close()
    return {"query": q, "platform": platform, "results": [dict(r) for r in rows]}


def _summary(rows):
    inn = sum(1 for r in rows if r["status"] == "in")
    oos = sum(1 for r in rows if r["status"] == "oos")
    na = sum(1 for r in rows if r["status"] == "na")
    reach = inn + oos
    prices = [r["price"] for r in rows if r.get("price") is not None]
    detail_counts: dict = {}
    for r in rows:
        d = r.get("detail")
        if d:
            detail_counts[d] = detail_counts.get(d, 0) + 1
    # b2b.html's tiles read total_units / mrp / avg_discount_pct / value_at_selling /
    # value_at_mrp. Without them "Total units on hand" and "Inventory value" rendered NA on
    # a fully SUCCESSFUL probe -- which reads as the tool being broken, on a product whose
    # whole pitch is per-store unit counts.
    avg_price = round(sum(prices) / len(prices)) if prices else None
    units = [r.get("qty") for r in rows if r["status"] == "in" and isinstance(r.get("qty"), int)]
    total_units = sum(units) if units else None
    mrps = [r["mrp"] for r in rows if r.get("mrp") is not None]
    mrp = round(sum(mrps) / len(mrps)) if mrps else None
    return {"in_stock": inn, "out_of_stock": oos, "na": na,
            "availability_pct": round(100 * inn / reach) if reach else None,
            "avg_price": avg_price,
            "detail_counts": detail_counts,
            "total_units": total_units, "mrp": mrp,
            "avg_discount_pct": round(100 * (mrp - avg_price) / mrp) if (mrp and avg_price) else None,
            "value_at_selling": total_units * avg_price if (total_units and avg_price) else None,
            "value_at_mrp": total_units * mrp if (total_units and mrp) else None}


@app.get("/api/b2b/inventory")
def inventory(platform: str = Query(...), product_id: str = Query(...),
              name: str = "", city: str = "Bengaluru", locality: str | None = None):
    stores_ = ds.stores_in_city(platform if platform in PLATS else "swiggy", city, locality)

    if platform == "swiggy":
        ck = ("swiggy", product_id, city, locality or "")
        stale_age = None
        hit = _SW_CACHE.get(ck)
        fresh = bool(hit and time.time() - hit[0] < _SW_TTL)
        if fresh:
            per = hit[1]
        else:
            # Serialise the cache-miss path and RE-CHECK after acquiring: without this, N
            # simultaneous clicks on the same product all miss and each fans out ~25 calls
            # to instamart.in. The gate additionally caps total concurrent fan-outs so
            # search + static serving keep their threadpool slots.
            with _SW_LOCK:
                hit = _SW_CACHE.get(ck)
                if hit and time.time() - hit[0] < _SW_TTL:
                    per, fresh = hit[1], True
                else:
                    with _SW_GATE:
                        try:
                            per = swiggy_probe.check(product_id, stores_) or []
                        except Exception:
                            per = []
                    # Only cache a run that actually REACHED Swiggy for at least one store.
                    # The old `if per:` cached all-NA runs too (a 12-element list of failures
                    # is truthy), so one throttled probe froze fake results for 15 minutes
                    # and could not self-heal.
                    if any(p.get("status") in ("in", "oos") for p in per):
                        if len(_SW_CACHE) > 2000:      # unbounded before; ~5 KB/entry
                            _SW_CACHE.clear()
                        _SW_CACHE[ck] = (time.time(), per)
                    elif hit:
                        # The fresh probe reached nothing (Swiggy throttles per IP, and every
                        # visitor's check leaves from the same server address -- so the busier
                        # this gets, the likelier this branch is). We still hold an older
                        # successful run: real numbers an hour old beat "couldn't verify"
                        # for stock levels, as long as we say how old they are.
                        age = time.time() - hit[0]
                        if age < _SW_STALE_MAX:
                            per, stale_age = hit[1], age
        by_id = {p["store_id"]: p for p in per}
        rows = []
        for s in stores_[:24]:
            p = by_id.get(s.store_id, {"status": "na", "detail": "not_scraped"})
            rows.append({"platform": "swiggy", "store_id": s.store_id, "name": s.name,
                         "locality": s.locality, "lat": s.lat, "lng": s.lng,
                         "status": p.get("status", "na"), "qty": p.get("qty"),
                         "price": p.get("price"), "mrp": p.get("mrp"),
                         "detail": p.get("detail") or "not_scraped"})
        return {"platform": "swiggy", "product_id": product_id, "name": name, "city": city,
                "locality": locality, "total_stores": len(stores_), "probed": len(rows),
                "stores": rows, "summary": _summary(rows), "cached": fresh,
                "stale_seconds": round(stale_age) if stale_age else None,
                "capped": len(stores_) > 24}

    # Blinkit -> fetched CLIENT-SIDE: hand the browser the stores + prid to probe itself
    return {"platform": "blinkit", "product_id": product_id, "name": name, "city": city,
            "locality": locality, "total_stores": len(stores_), "client_side": True,
            "capped": len(stores_) > 40,
            "stores": [{"platform": "blinkit", "store_id": s.store_id, "name": s.name,
                        "locality": s.locality, "lat": s.lat, "lng": s.lng, "status": "pending"}
                       for s in stores_[:40]]}


@app.get("/")
def root():
    # frontend/ has no index.html (only b2b.html) -- a bare share link should still work
    return RedirectResponse("/b2b.html")


if FRONTEND.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="frontend")
