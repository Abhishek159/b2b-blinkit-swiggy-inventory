"""Swiggy per-store live probe for the B2B tool — instamart.in, no browser.

Reuses the datacenter-proven mint + cart mechanics (works from a Render IP) to
probe ONE product across many Swiggy stores by numeric store_id, returning each
store's stock + price. Triples come from the bundled catalog_lite.db.
"""
from __future__ import annotations

import json
import math
import random
import re
import sqlite3
import time
import urllib.parse
from pathlib import Path

from curl_cffi import requests

HERE = Path(__file__).resolve().parent
DB = HERE / "data" / "catalog_lite.db"
HOST = "https://instamart.in"
SELECT_LOC = HOST + "/api/instamart/home/select-location/v2"
CART = HOST + "/api/instamart/checkout/v2/cart?pageType=INSTAMART_CART"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_session = None
_TTL = 45 * 60


def _headers(extra=None):
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-IN,en;q=0.9",
         "sec-ch-ua": '"Google Chrome";v="124", "Chromium";v="124", "Not_A Brand";v="24"',
         "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"'}
    if extra:
        h.update(extra)
    return h


def _mint():
    global _session
    if _session and time.time() - _session["ts"] < _TTL:
        return _session
    try:
        s = requests.Session()
        html = s.get(HOST + "/", headers=_headers(), timeout=10, impersonate="chrome124").text or ""
    except Exception:
        return None
    m = (re.findall(r'matcher["\']?\s*[:=]\s*["\']([^"\']{4,60})', html, re.I) or [""])[0]
    b = (re.findall(r'buildVersion["\']?\s*[:=]\s*["\']([^"\']{1,30})', html, re.I) or [""])[0]
    if not m:
        return None
    _session = {"jar": dict(s.cookies), "matcher": m, "build": b, "ts": time.time()}
    return _session


def _triple(product_id: str):
    """(product_id, parent_id, spin_id, mrp) for a Swiggy product."""
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        r = con.execute("SELECT product_id, parent_id, spin_id, mrp FROM products "
                        "WHERE platform='swiggy' AND product_id=? LIMIT 1", (str(product_id),)).fetchone()
        con.close()
        return r
    except sqlite3.Error:
        return None


def _inner_status(o):
    """Instamart answers HTTP 200 with an empty item set and a NON-ZERO inner statusCode
    (150 "cart validation failed", 192, 161...) when it throttles or rejects a batch. The
    outer envelope still reads 0/200, so status_code alone cannot tell a throttle from a
    genuine "this store doesn't carry it" -- and calling a throttle "not carried" publishes
    a fabricated confirmed-zero. Deepest non-zero wins."""
    seen = []

    def walk(x):
        if isinstance(x, dict):
            v = x.get("statusCode")
            if isinstance(v, int):
                seen.append(v)
            for y in x.values():
                walk(y)
        elif isinstance(x, list):
            for y in x:
                walk(y)
    walk(o)
    return next((v for v in seen if v not in (0, 200)), 0)


_JITTER_MIN_M = 50.0
_JITTER_MAX_M = 70.0
_MAX_ATTEMPTS = 3


def _digits(v) -> str:
    return re.sub(r"\D", "", str(v))


def _jitter(lat: float, lng: float, attempt: int):
    """Nudge the pin 50-70 m in a direction that rotates per attempt.

    Two reasons. Probing the exact same coordinate for every check is a fingerprint;
    and when a warehouse hands the pin to a neighbour, re-asking from a slightly
    different side is what tells us whether that hand-off is real or incidental.
    Attempts fan out ~120 degrees apart so a retry samples a genuinely different side.
    """
    dist = random.uniform(_JITTER_MIN_M, _JITTER_MAX_M)
    ang = random.uniform(0, 2 * math.pi) if attempt == 0 else \
        (attempt * 2 * math.pi / _MAX_ATTEMPTS) + random.uniform(-0.35, 0.35)
    dlat = (dist * math.cos(ang)) / 111320.0
    dlng = (dist * math.sin(ang)) / (111320.0 * math.cos(math.radians(lat)) or 1e-9)
    return lat + dlat, lng + dlng


def _probe_one(sess, lat, lng, store_id, item, spin):
    """One store, one product -> (matched cart node, None) | (None, reason).

    reason distinguishes an honest "this store's cart doesn't carry it" (HTTP 200
    both calls, no matching node — Swiggy itself confirming absence) from an actual
    technical failure (network exception / non-200), which earlier code conflated
    into a single "na" bucket and the UI then mislabeled as "unreachable" even when
    it was a confirmed non-stock result.
    """
    jar = dict(sess["jar"])
    jar["lat"] = "s%3A" + str(lat)
    jar["lng"] = "s%3A" + str(lng)
    jar["userLocation"] = urllib.parse.quote(json.dumps({"address": "", "lat": lat, "lng": lng, "id": "", "name": ""}))
    for k in ("LocSrc", "address", "addressId"):
        jar.pop(k, None)
    headers = _headers({
        "Content-Type": "application/json", "Origin": HOST, "Referer": HOST + "/instamart",
        "Cookie": "; ".join(f"{k}={v}" for k, v in jar.items()),
        "sid": jar.get("sid", ""), "tid": jar.get("tid", ""), "deviceId": jar.get("deviceId", ""),
        "matcher": sess["matcher"], "x-build-version": sess["build"]})
    loc = json.dumps({"data": {"lat": lat, "lng": lng, "address": "", "addressId": "",
                               "annotation": "", "clientId": "INSTAMART-APP"}})
    try:
        rl = requests.post(SELECT_LOC, headers=headers, data=loc, impersonate="chrome124", timeout=8)
        if rl.status_code != 200:
            return None, "failed", None
        for k, v in dict(rl.cookies).items():
            jar[k] = v
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())
    except Exception:
        return None, "failed", None
    sid = re.sub(r"\D", "", str(store_id)) or "0"
    pid, parent = item[0], item[1]
    cart_items = [{"productId": parent, "quantity": 1, "itemId": pid, "spin": spin,
                   "meta": {"type": "structure", "storeId": int(sid)}, "serviceLine": "INSTAMART"}]
    payload = {"data": {"items": cart_items,
                        "cartMetaData": {"deliveryType": "INSTANT", "owner": "APP",
                                         "primaryStoreId": int(sid), "storeIds": [int(sid)]},
                        "cartType": "INSTAMART"},
               "source": "userInitiated",
               "cartAnalyticsMetaInfo": {"actionType": "ADD_ITEMS", "itemInfo": {
                   "itemId": pid, "quantity": 1, "spin": spin, "productId": parent}}}
    try:
        r = requests.post(CART, headers=headers, data=json.dumps(payload), impersonate="chrome124", timeout=12)
        if r.status_code != 200:
            return None, "failed", None
        data = r.json()
    except Exception:
        return None, "failed", None
    found = {}

    def walk(o):
        if isinstance(o, dict):
            if o.get("spin") and ("inventory" in o or "price" in o):
                found[str(o["spin"])] = o
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)

    # Which store did Swiggy ACTUALLY serve? meta.storeId is advisory: the serving store
    # is derived from the pinned lat/lng, so a closed or non-serving warehouse is silently
    # handed off to a neighbour -- and its stock came back labelled as the pinned store's.
    served = set()

    def store_ids(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "storeId" and str(v).isdigit():
                    served.add(str(v))
                store_ids(v)
        elif isinstance(o, list):
            for v in o:
                store_ids(v)
    store_ids(data)
    served_id = next(iter(served)) if len(served) == 1 else None

    node = found.get(str(spin)) or (next(iter(found.values())) if found else None)
    if node:
        return node, None, served_id
    return ((None, "throttled", served_id) if _inner_status(data)
            else (None, "not_carried", served_id))


def check(product_id: str, stores: list, cap: int = 24, budget: float = 55.0):
    """stores: list of dark_stores Store objects. Returns per-store stock/price.

    `cap` covers a WHOLE area rather than sampling it. The largest Instamart area in
    the data holds 21 stores, so 24 reaches 100% of them; the median area holds 1, so
    this costs nothing on the typical check. Sampling 12 of 22 and presenting the result
    as the area's stock was simply wrong information.

    `budget` caps total wall-clock. Without it the worst case is 12 stores x (8s + 12s)
    plus the mint = ~250s+, but the endpoint writes no bytes until it finishes and Fly's
    proxy severs an idle connection at 60s -- so a couple of slow stores turned into
    "Couldn't reach the API" for the visitor. Stores past the deadline report
    not_scraped ("timed out / not scraped"), which is honest rather than a fake zero."""
    deadline = time.time() + budget
    trip = _triple(product_id)
    sess = _mint()
    if not trip or not sess:
        return None
    pid, parent, spin, mrp0 = trip
    item = (pid, parent)

    def row(store_id, node, detail_reason=None, pinned=None, served=None, attempts=1):
        if node is None:
            r = {"store_id": store_id, "status": "na", "detail": detail_reason or "failed"}
        else:
            inv = node.get("inventory", {}) or {}
            pr = node.get("price", {}) or {}
            instock = bool(inv.get("in_stock"))
            # inv["total"] is the true stock count. NOT cart_allowed_quantity /
            # max_allowed_quantity -- those are per-order purchase caps.
            r = {"store_id": store_id,
                 "status": "in" if instock else "oos",
                 "qty": inv.get("total") if instock else 0,
                 "price": pr.get("offer_price"), "mrp": pr.get("mrp") or mrp0,
                 "detail": "matched" if instock else "zero_stock"}
        if pinned and _digits(pinned) != _digits(store_id):
            r["pinned_from"] = pinned          # we aimed at one store, this one answered
        r["attempts"] = attempts
        if served:
            r["served_store_id"] = served
        return r

    resolved: dict = {}     # store_id that the data ACTUALLY belongs to -> row
    unresolved: list = []   # pinned stores we never got a reading for

    for s_ in stores[:cap]:
        sid = _digits(s_.store_id)
        if sid in resolved:
            continue        # a previous pin already produced this store's reading
        last_served, last_node, last_reason = None, None, None
        got = False

        for attempt in range(_MAX_ATTEMPTS):
            if time.time() > deadline:
                last_reason = "not_scraped"
                break
            jlat, jlng = _jitter(s_.lat, s_.lng, attempt)
            node, reason, served = _probe_one(sess, jlat, jlng, s_.store_id, item, spin)
            served = _digits(served) if served else None
            last_reason = reason or last_reason
            time.sleep(0.3)

            if served and served == sid:
                resolved[sid] = row(sid, node, reason, pinned=sid, served=served,
                                    attempts=attempt + 1)
                got = True
                break

            if served:
                # A neighbour answered. Re-ask from a different side: if the SAME
                # neighbour answers again, the hand-off is real and its stock is the
                # honest answer for this point -- for a high-value SKU that IS how
                # Swiggy fulfils. One-off disagreement means keep looking.
                if served == last_served:
                    if served not in resolved:
                        resolved[served] = row(served, node, reason, pinned=sid,
                                               served=served, attempts=attempt + 1)
                    got = True
                    break
                last_served, last_node = served, node
                continue

            if node is not None:            # answered, but told us no store id
                resolved[sid] = row(sid, node, reason, pinned=sid, attempts=attempt + 1)
                got = True
                break
            if reason in ("throttled", "failed"):
                break                        # retrying a throttle only deepens it

        if not got:
            # accept a single-sighting neighbour rather than discard a real reading
            if last_served and last_node is not None and last_served not in resolved:
                resolved[last_served] = row(last_served, last_node, None, pinned=sid,
                                            served=last_served, attempts=_MAX_ATTEMPTS)
            else:
                unresolved.append((sid, last_reason))

    out = list(resolved.values())
    # every pinned store we never resolved is reported explicitly as unavailable data,
    # never as zero -- "we could not read this store" is not "this store has none".
    for sid, reason in unresolved:
        if sid not in resolved:
            out.append({"store_id": sid, "status": "na",
                        "detail": reason if reason in ("throttled", "not_scraped", "failed",
                                                       "not_carried") else "not_available"})

    # Run-level sanity guard: if not ONE store matched across the whole fan-out, that is a
    # probe failure (throttle, rotated build, dead session) -- not 12 independent confirmed
    # absences. Publishing the latter as "not carried (confirmed)" is a fabricated claim.
    if out and all(r.get("detail") == "not_carried" for r in out):
        out = [{"store_id": r["store_id"], "status": "na", "detail": "throttled"} for r in out]
    return out
