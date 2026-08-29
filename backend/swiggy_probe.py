"""Swiggy per-store live probe for the B2B tool — instamart.in, no browser.

Reuses the datacenter-proven mint + cart mechanics (works from a Render IP) to
probe ONE product across many Swiggy stores by numeric store_id, returning each
store's stock + price. Triples come from the bundled catalog_lite.db.
"""
from __future__ import annotations

import json
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
        html = s.get(HOST + "/", headers=_headers(), timeout=30, impersonate="chrome124").text or ""
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


def _probe_one(sess, lat, lng, store_id, item, spin):
    """One store, one product -> the matched cart node (has inventory/price) or None."""
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
        rl = requests.post(SELECT_LOC, headers=headers, data=loc, impersonate="chrome124", timeout=20)
        for k, v in dict(rl.cookies).items():
            jar[k] = v
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())
    except Exception:
        return None
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
        r = requests.post(CART, headers=headers, data=json.dumps(payload), impersonate="chrome124", timeout=25)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:
        return None
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
    return found.get(str(spin)) or (next(iter(found.values())) if found else None)


def check(product_id: str, stores: list, cap: int = 12):
    """stores: list of dark_stores Store objects. Returns per-store stock/price."""
    trip = _triple(product_id)
    sess = _mint()
    if not trip or not sess:
        return None
    pid, parent, spin, mrp0 = trip
    item = (pid, parent)
    out = []
    for s in stores[:cap]:
        node = _probe_one(sess, s.lat, s.lng, s.store_id, item, spin)
        if node is None:
            out.append({"store_id": s.store_id, "status": "na"})
            continue
        inv = node.get("inventory", {}) or {}
        pr = node.get("price", {}) or {}
        instock = bool(inv.get("in_stock"))
        out.append({"store_id": s.store_id,
                    "status": "in" if instock else "oos",
                    "qty": inv.get("quantity") if instock else 0,
                    "price": pr.get("offer_price"), "mrp": pr.get("mrp") or mrp0})
        time.sleep(0.3)   # pace
    return out
