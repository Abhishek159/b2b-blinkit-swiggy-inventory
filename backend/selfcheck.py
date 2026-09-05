"""Fail the Docker build if the image is missing anything the app needs at runtime.

Every check here corresponds to a failure that is INVISIBLE to /api/health -- the app
boots and answers 200 while the site itself is broken. Runs at build time only.
"""
import pathlib
import sqlite3
import sys

fe = pathlib.Path("/app/frontend/b2b.html")
assert fe.exists(), "frontend/b2b.html missing -> every page 404s while health stays green"

db = pathlib.Path("/app/backend/data/catalog_lite.db")
assert db.exists(), "catalog_lite.db missing -> 500 on every search, all-NA Swiggy"

con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
products = con.execute("select count(*) from products").fetchone()[0]
assert products > 120000, f"products table thin: {products}"
# FTS5 is a compile-time SQLite option; a base image built without it fails only here.
fts = con.execute("select count(*) from search where search match 'milk*'").fetchone()[0]
assert fts > 0, "FTS5 unavailable or search index empty"

sd = pathlib.Path("/app/backend/data/Online Dark store Data")
assert sd.is_dir(), "space-named store dir missing -> city coverage collapses"
for f in ("blinkit-darkstores.csv", "swiggy-darkstores.csv"):
    assert (sd / f).exists(), f"{f} missing"

print(f"image ok: products={products} fts_hits={fts}", file=sys.stderr)
