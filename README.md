# B2B Blinkit + Swiggy inventory (deployable, no Zepto, no login)

A datacenter-deployable cut of the B2B store-level tool: **Blinkit + Swiggy only**,
open to everyone, self-contained (fits GitHub, no big-DB hosting hack).

## Run locally
```bash
cd backend
pip install -r requirements.txt
uvicorn app:app --port 8010
# open http://localhost:8010/b2b.html
```

## Deploy (Render)
`New + → Blueprint → point at this repo`. Free plan. Live at `…onrender.com`.

## What's solved ✅
- **The database blocker.** The original tool needed `catalog.db` (425 MB) + `product_master.db`
  (1.5 GB) — both over GitHub's 100 MB limit. Replaced by one bundled
  **`backend/data/catalog_lite.db` (45 MB)**: 124,296 Blinkit+Swiggy products with an
  **FTS5 search index** *and* the Swiggy cart triples (spin/item/parent). Commits straight to GitHub.
- **Self-contained data** (`backend/data/`, ~47 MB): the lite catalog + store CSVs + KML +
  national darkstore CSVs. Nothing is downloaded or hosted elsewhere.
- **Working server-side today:** product search, city/area lists, and the store map
  (`/api/b2b/cities`, `/localities`, `/stores`, `/catalog-search`). Verified running.
- **No login.**

## What's left ⚠️ (the live inventory probes)
1. **Swiggy live** — wire the instamart.in cart probe into `/api/b2b/inventory` (works from a
   datacenter IP). The Swiggy triples it needs are already in `catalog_lite.db`. Carry over the
   zone-price cache (stub already in `app.py`).
2. **Blinkit live** — move the per-store check **client-side** into `frontend/b2b.html` (Blinkit
   is 403 from a datacenter but open-CORS from the visitor's browser). Throttle the fan-out.
3. **Frontend adaptation** — `frontend/b2b.html` is copied from the local tool as-is; still needs:
   remove the **Zepto** toggle, remove the **login gate** (call the API without a token), and route
   Blinkit "Check availability" to the client-side fetch.

Until (1)–(3) land, `/api/b2b/inventory` returns the candidate stores with `status:"na"` and a note.

## Architecture
```
visitor browser (b2b.html): Blinkit inventory fetched here (their IP) · search/map/Swiggy via API
Render web service (backend/app.py): search + stores + Swiggy probe, from backend/data/*
```
