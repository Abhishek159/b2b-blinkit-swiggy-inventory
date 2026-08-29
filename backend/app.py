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
import time
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import dark_stores as ds

HERE = Path(__file__).resolve().parent
DB = HERE / "data" / "catalog_lite.db"
FRONTEND = HERE.parent / "frontend"
PLATS = ("blinkit", "swiggy")   # Zepto dropped

app = FastAPI(title="B2B Blinkit + Swiggy inventory")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# zone-price cache (prices ~city-uniform): one live Swiggy call per (query,~11km cell)
_SW_CACHE: dict = {}
_SW_TTL = 15 * 60


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
def catalog_search(q: str = Query(..., min_length=1), platform: str | None = None, limit: int = 24):
    con = _db()
    match = " ".join(t + "*" for t in q.split() if t)
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
    return {"in_stock": inn, "out_of_stock": oos, "na": na,
            "availability_pct": round(100 * inn / reach) if reach else None,
            "avg_price": round(sum(prices) / len(prices)) if prices else None,
            "detail_counts": {"zero_stock": oos}}


@app.get("/api/b2b/inventory")
def inventory(platform: str = Query(...), product_id: str = Query(...),
              name: str = "", city: str = "Bengaluru", locality: str | None = None):
    stores_ = ds.stores_in_city(platform if platform in PLATS else "swiggy", city, locality)

    if platform == "swiggy":
        ck = ("swiggy", product_id, city, locality or "")
        hit = _SW_CACHE.get(ck)
        if hit and time.time() - hit[0] < _SW_TTL:
            per = hit[1]
        else:
            import swiggy_probe
            per = swiggy_probe.check(product_id, stores_) or []
            if per:
                _SW_CACHE[ck] = (time.time(), per)
        by_id = {p["store_id"]: p for p in per}
        rows = []
        for s in stores_[:12]:
            p = by_id.get(s.store_id, {"status": "na"})
            rows.append({"platform": "swiggy", "store_id": s.store_id, "name": s.name,
                         "locality": s.locality, "lat": s.lat, "lng": s.lng,
                         "status": p.get("status", "na"), "qty": p.get("qty"),
                         "price": p.get("price"), "mrp": p.get("mrp"), "detail": "zero_stock"})
        return {"platform": "swiggy", "product_id": product_id, "name": name, "city": city,
                "locality": locality, "total_stores": len(stores_), "probed": len(rows),
                "stores": rows, "summary": _summary(rows), "cached": bool(hit)}

    # Blinkit -> fetched CLIENT-SIDE: hand the browser the stores + prid to probe itself
    return {"platform": "blinkit", "product_id": product_id, "name": name, "city": city,
            "locality": locality, "total_stores": len(stores_), "client_side": True,
            "stores": [{"platform": "blinkit", "store_id": s.store_id, "name": s.name,
                        "locality": s.locality, "lat": s.lat, "lng": s.lng, "status": "pending"}
                       for s in stores_[:20]]}


if FRONTEND.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="frontend")
