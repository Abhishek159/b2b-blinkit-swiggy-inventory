"""Unified dark-store map for all three players.

Reads the three root CSVs (blinkit.csv, zepto.csv, swiggy.csv), each a dump of a
player's dark stores with lat/lng, and gives everyone one clean view:

  - nearest(platform, lat, lng)     -> (Store, km)   [serviceability + ETA]
  - stores_in_city(platform, city)  -> [Store]       [B2B city-wide sweep]
  - cities(platform) / all_cities() -> [str]
  - localities(city)                -> [str]          [B2B sub-layers: HSR, ...]

City is inferred from lat/lng via metro bounding boxes (robust — the CSVs' own
city columns are half-empty). Locality (HSR, Koramangala, ...) is the nearest
named centroid within 3.5 km, so B2B can drill from city -> sub-area.
"""
from __future__ import annotations

import csv
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from math import radians, sin, cos, asin, sqrt
from pathlib import Path

_ROOT = Path(__file__).resolve().parent / "data"   # self-contained: bundled store data

# metro bounding boxes: name -> (lat_min, lat_max, lng_min, lng_max)
_METROS = {
    "Bengaluru":   (12.70, 13.20, 77.35, 77.85),
    "Delhi NCR":   (28.30, 28.90, 76.90, 77.55),
    "Mumbai":      (18.85, 19.35, 72.75, 73.05),
    "Hyderabad":   (17.20, 17.60, 78.20, 78.70),
    "Chennai":     (12.85, 13.25, 80.10, 80.35),
    "Pune":        (18.40, 18.70, 73.70, 74.05),
    "Kolkata":     (22.45, 22.75, 88.25, 88.50),
    "Ahmedabad":   (22.95, 23.15, 72.45, 72.75),
    "Gurugram":    (28.35, 28.55, 76.95, 77.15),
}

# Locality centroids for the B2B sub-layer drill-down, one dict per city.
#
# Bengaluru's was hand-curated. The other 7 were discovered, not guessed:
# build_localities.py (2026-08-12) sampled real dark-store coordinates spread
# across each city and reverse-geocoded them through OpenStreetMap's Nominatim,
# so every name here is tied to an actual store location and an actual
# suburb/neighbourhood name Nominatim resolved for it - never invented. Re-run
# that script and paste fresh output here if the store lists change materially.
#
# Gurugram has NO entry and never will from that script: its bounding box in
# _METROS sits entirely inside Delhi NCR's, and _city_for() matches Delhi NCR
# first, so every Gurugram-area store already classifies as Delhi NCR before
# a Gurugram-specific locality pass could ever see it. That is a city-boundary
# decision, not a data gap - resolving it means changing _METROS, not this dict.
_LOCALITIES = {
    "Bengaluru": {
        "HSR Layout":       (12.9116, 77.6474),
        "Koramangala":      (12.9352, 77.6245),
        "Indiranagar":      (12.9719, 77.6412),
        "Whitefield":       (12.9698, 77.7500),
        "Marathahalli":     (12.9569, 77.7011),
        "Electronic City":  (12.8399, 77.6770),
        "Jayanagar":        (12.9250, 77.5938),
        "BTM Layout":       (12.9166, 77.6101),
        "Bellandur":        (12.9260, 77.6762),
        "Hebbal":           (13.0358, 77.5970),
        "Rajajinagar":      (12.9915, 77.5550),
        "Yelahanka":        (13.1007, 77.5963),
    },
    "Delhi NCR": {
        "Amberhai": (28.5775, 77.0478),
        "Brahma City": (28.4051, 77.092),
        "Hans Enclave": (28.4351, 77.0334),
        "IFFCO Chowk": (28.4627, 77.0875),
        "Najafgarh": (28.5971, 77.0666),
        "Old Delhi": (28.6415, 77.2346),
        "Old Faridabad": (28.4263, 77.3396),
        "Paharganj": (28.6587, 77.2024),
        "Pitampura": (28.6904, 77.1343),
        "Punjabi Bagh": (28.6618, 77.1529),
        "Rohini": (28.716, 77.135),
        "Saket": (28.5225, 77.215),
        "Sector 52": (28.4374, 77.0727),
        "Sector 6": (28.4789, 77.0249),
        "Sector 62": (28.6188, 77.3782),
        "Shakarpur": (28.6283, 77.2787),
        "Sohna": (28.4241, 77.1468),
        "South Extension": (28.5733, 77.2281),
        "Trilok Puri": (28.608, 77.2925),
        "Vasant Kunj": (28.5252, 77.1545),
    },
    "Mumbai": {
        "Airoli": (19.144, 72.9906),
        "Amrut Nagar": (19.1653, 73.0263),
        "Anand Nagar": (19.2657, 72.9678),
        "Andheri East": (19.1021, 72.851),
        "Bhandup East": (19.1395, 72.9288),
        "Bhandup West": (19.1538, 72.9356),
        "Borivali East": (19.2264, 72.8603),
        "Chandivali": (19.1113, 72.893),
        "Fort": (18.9377, 72.8333),
        "Ghatkopar West": (19.0863, 72.9018),
        "Govandi West": (19.0499, 72.9166),
        "Indira Nagar": (19.0332, 72.8615),
        "Jambli Naka": (19.2007, 73.0003),
        "Jogeshwari West": (19.1418, 72.8347),
        "Juinagar": (19.05, 73.0206),
        "Khar": (19.0625, 72.8282),
        "Lalbaug": (18.9896, 72.8405),
        "Lower Parel": (19.0009, 72.8302),
        "MIDC": (19.1317, 72.8725),
        "Matunga East": (19.0126, 72.8499),
        "Matunga West": (19.0311, 72.8443),
        "Mira": (19.2995, 72.8681),
        "R/C Ward": (19.2134, 72.8429),
        "Saki Naka": (19.1113, 72.8843),
        "Thakur Village": (19.2098, 72.8713),
        "Turbhe": (19.0848, 73.0092),
        "Versova": (19.1282, 72.82),
        "Wagle Industrial Estate": (19.1962, 72.9459),
    },
    "Hyderabad": {
        "Borabanda": (17.4509, 78.4226),
        "Financial District": (17.4118, 78.3428),
        "Gaddiannaram": (17.3617, 78.5222),
        "HITEC City": (17.455, 78.3966),
        "Jubilee Hills": (17.4318, 78.4315),
        "Khilwat": (17.3539, 78.4716),
        "Kokapet": (17.3838, 78.3414),
        "Kukatpally": (17.4992, 78.4139),
        "LB Nagar": (17.3521, 78.5543),
        "Mallampet": (17.5585, 78.3613),
        "Manikonda": (17.4012, 78.3726),
        "Medpally": (17.4091, 78.6),
        "Moula Ali": (17.4688, 78.5458),
        "Nacharam": (17.4335, 78.5556),
        "Narsingi": (17.393, 78.3765),
        "Pragathi Nagar": (17.5232, 78.3945),
        "Saket": (17.4953, 78.5649),
        "Ward 10 Uppal": (17.407, 78.5694),
        "Ward 114 KPHB Colony": (17.4854, 78.3895),
        "Ward 115 Balaji Nagar": (17.4686, 78.4135),
        "Ward 130 Subash Nagar": (17.5068, 78.4666),
        "Ward 133 Macha Bolarum": (17.5143, 78.4898),
        "Ward 4 Meerpet H.B Colony": (17.4469, 78.573),
        "Ward 41 Kanchanbagh": (17.3227, 78.4888),
        "Ward 57 Suleman Nagar": (17.3493, 78.4178),
    },
    "Chennai": {
        "Adyar": (12.9757, 80.2181),
        "Anna Nagar": (13.0917, 80.2168),
        "Chromepet": (12.9454, 80.136),
        "Gerugambakkam": (13.0164, 80.1389),
        "JB Estate": (13.0989, 80.1083),
        "Kannadapalayam": (12.9262, 80.1113),
        "Karayanchavadi": (13.0461, 80.1174),
        "Keelkattalai": (12.9543, 80.186),
        "Kodambakkam": (13.0334, 80.2202),
        "Korattur": (13.1253, 80.1949),
        "Madhavaram Milk Colony": (13.1637, 80.2546),
        "Mangadu": (13.031, 80.114),
        "Moovarasampettai": (12.9684, 80.1893),
        "Nolambur": (13.077, 80.1685),
        "Noothanchery": (12.9162, 80.1522),
        "Pammal": (12.9683, 80.1335),
        "Perungudi": (12.9279, 80.2002),
        "Sholinganallur": (12.9306, 80.2326),
        "Teynampet": (13.0561, 80.253),
        "Thiru. Vi. Ka. Nagar": (13.1148, 80.2309),
        "Thiruvanchery": (12.899, 80.1411),
        "Tondiarpet": (13.1272, 80.2581),
        "Ward 148": (13.0579, 80.1797),
        "Ward 152": (13.0294, 80.1704),
        "Ward 87": (13.0991, 80.1851),
    },
    "Pune": {
        "Aundh": (18.5504, 73.8098),
        "Baner": (18.5562, 73.7713),
        "Bavdhan Budruk": (18.5145, 73.7689),
        "Bhujbal Chowk": (18.5973, 73.765),
        "Ghorpuri": (18.5253, 73.9006),
        "Karve Nagar": (18.5039, 73.815),
        "Katraj": (18.456, 73.8666),
        "Keshav Nagar": (18.5338, 73.9412),
        "Kondhwa": (18.4608, 73.8806),
        "Krushna Nagar": (18.48, 73.9298),
        "Pimple Saudagar": (18.5937, 73.8037),
        "Sahakar Nagar": (18.486, 73.8585),
        "Shivane": (18.4556, 73.79),
        "Vishal Nagar": (18.5776, 73.7884),
        "Wakad gaav": (18.5779, 73.7492),
        "Ward 1": (18.5798, 73.885),
        "Warje": (18.4893, 73.7938),
    },
    "Kolkata": {
        "Action Area I": (22.5777, 88.4611),
        "Baghajatin": (22.4834, 88.3889),
        "Ballygunge": (22.5245, 88.3716),
        "Belgharia": (22.6715, 88.3784),
        "Bhowanipore": (22.5287, 88.3518),
        "Bikramgarh": (22.494, 88.3634),
        "Charu Market Area": (22.5081, 88.3535),
        "Cossipore": (22.6258, 88.382),
        "Dunlop": (22.6651, 88.37),
        "Ho Chi Minh Sarani": (22.5442, 88.3511),
        "Howrah Maidan": (22.5735, 88.3284),
        "Keshtopur": (22.5961, 88.4336),
        "Kushtia": (22.5231, 88.3859),
        "Machua Bazar": (22.5824, 88.3648),
        "Moore Avenue": (22.4658, 88.3357),
        "Narkeldanga": (22.5745, 88.3998),
        "New Alipore": (22.5171, 88.3236),
        "Rajarhat Gopalpur": (22.6211, 88.4415),
        "Shanti Nagar": (22.4683, 88.3735),
        "Simla": (22.5899, 88.3758),
        "Tangra North": (22.5452, 88.3873),
    },
    "Ahmedabad": {
        "Ambawadi": (23.023, 72.5567),
        "Bhagvat": (23.0974, 72.542),
        "Bodakdev": (23.0377, 72.5119),
        "Chanakyapuri": (23.0965, 72.5497),
        "Gulbai tekra": (23.0385, 72.5532),
        "Jodhpur": (23.0169, 72.5189),
        "Madhupura": (23.0432, 72.5893),
        "Makarba": (22.9963, 72.5109),
        "Naranpura": (23.0589, 72.5388),
        "Narol gam": (22.9528, 72.6003),
        "New Ranip": (23.0882, 72.5621),
        "Rajpur Gomtipur": (23.0089, 72.6154),
        "Sola": (23.0795, 72.5073),
        "Thaltej": (23.0464, 72.5042),
        "Usmanpura": (23.0439, 72.5579),
        "Vastral": (22.9922, 72.6535),
        "Vastrapur": (23.0284, 72.5302),
    },
}
_LOCALITY_RADIUS_KM = 3.5


@dataclass
class Store:
    platform: str
    store_id: str
    name: str
    lat: float
    lng: float
    city: str
    locality: str | None


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    lng1, lat1, lng2, lat2 = map(radians, [lng1, lat1, lng2, lat2])
    dlng, dlat = lng2 - lng1, lat2 - lat1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlng / 2) ** 2
    return 6371 * 2 * asin(sqrt(a))


def _city_for(lat: float, lng: float) -> str:
    for name, (la0, la1, lo0, lo1) in _METROS.items():
        if la0 <= lat <= la1 and lo0 <= lng <= lo1:
            return name
    return "Other"


def _locality_for(city: str, lat: float, lng: float, radius: float = _LOCALITY_RADIUS_KM) -> str | None:
    cents = _LOCALITIES.get(city)
    if not cents:
        return None
    best, best_km = None, None
    for name, (cla, clo) in cents.items():
        d = haversine_km(lat, lng, cla, clo)
        if best_km is None or d < best_km:
            best, best_km = name, d
    return best if best_km is not None and best_km <= radius else None


def _load_csv(path: Path, platform: str, id_col, name_col) -> list[Store]:
    stores: list[Store] = []
    if not path.exists():
        return stores
    seen: set[str] = set()   # drop duplicate + id-less rows so each store is probed once
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            try:
                lat = float(row.get("latitude") or row.get("Latitude") or "")
                lng = float(row.get("longitude") or row.get("Longitude") or "")
            except (TypeError, ValueError):
                continue
            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                continue
            city = _city_for(lat, lng)
            sid = ""
            for c in id_col:
                if row.get(c):
                    sid = str(row[c]).split(".")[0].strip()
                    break
            # swiggy.csv carries 108 duplicate store_ids (same warehouse twice) and
            # zepto.csv 141 rows with no store_id at all; both make per-store counts
            # and 1:1 manual lookup unreliable. Keep the first of each real id only.
            if not sid or sid in seen:
                continue
            seen.add(sid)
            nm = ""
            for c in name_col:
                if row.get(c):
                    nm = str(row[c]).strip()
                    break
            stores.append(Store(
                platform=platform, store_id=sid, name=nm or platform.title(),
                lat=lat, lng=lng, city=city,
                locality=_locality_for(city, lat, lng),
            ))
    return stores


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-", re.I)
_NUM_RE = re.compile(r"^\d+(\.0)?$")


def _kml_platform(fname: str) -> str | None:
    f = (fname or "").lower()
    if "zepto" in f:
        return "zepto"
    if "swiggy" in f or "instamart" in f:
        return "swiggy"
    if "blinkit" in f:
        return "blinkit"
    return None


def _load_kml(path: Path) -> dict[str, list[Store]]:
    """Parse the darkstoremap.kml export (folders grouped by platform) into the
    same Store shape as the CSVs. Store ids share the CSVs' id space — Zepto UUID,
    Swiggy numeric, Blinkit merchant_id — so downstream dedup by store_id works."""
    out: dict[str, list[Store]] = {"blinkit": [], "zepto": [], "swiggy": []}
    if not path.exists():
        return out
    try:
        raw = re.sub(r'\sxmlns(:\w+)?="[^"]+"', "", path.read_text(encoding="utf-8", errors="replace"))
        root = ET.fromstring(raw)
    except Exception:
        return out
    tag = lambda el: el.tag.split("}")[-1]
    for folder in root.iter("Folder"):
        plat = _kml_platform(folder.findtext("name") or "")
        if not plat:
            continue
        for pm in [p for p in folder if tag(p) == "Placemark"]:
            ed = {d.get("name"): (d.findtext("value") or "") for d in pm.iter("Data")}
            nm = pm.findtext("name") or ""
            sid = ed.get("Store ID") or ed.get("merchant_id") or ed.get("backend_merchant_id") or ""
            if not sid and (_UUID_RE.match(nm) or _NUM_RE.match(nm)):
                sid = nm
            sid = str(sid).split(".")[0].strip()
            lat = lng = None
            ctxt = pm.findtext(".//coordinates")
            if ctxt:
                parts = ctxt.strip().split(",")
                if len(parts) >= 2:
                    try:
                        lng, lat = float(parts[0]), float(parts[1])
                    except ValueError:
                        pass
            if lat is None:
                try:
                    lat = float(ed.get("Latitude") or ed.get("lat"))
                    lng = float(ed.get("Longitude") or ed.get("lon"))
                except (TypeError, ValueError):
                    pass
            if not sid or lat is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
                continue
            name = (ed.get("Store Name") or ed.get("backend_merchant_name") or ed.get("merchant_name")
                    or (nm if not _UUID_RE.match(nm) and not _NUM_RE.match(nm) else ""))
            city = _city_for(lat, lng)
            out[plat].append(Store(platform=plat, store_id=sid, name=name or plat.title(),
                                   lat=lat, lng=lng, city=city, locality=_locality_for(city, lat, lng)))
    return out


_DS_DIR = _ROOT / "Online Dark store Data"   # national per-platform darkstore CSV dumps


def _load_ds_csv(path: Path, platform: str, name_field: str | None) -> list[Store]:
    """Load an Online-Dark-store-Data CSV (columns: id, lat, lng, +optional
    name/locality). Same store-id space as the other sources, so dedup by id."""
    out: list[Store] = []
    if not path.exists():
        return out
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            sid = str(row.get("id") or "").split(".")[0].strip()
            try:
                lat, lng = float(row["lat"]), float(row["lng"])
            except (TypeError, ValueError, KeyError):
                continue
            if not sid or not (-90 <= lat <= 90 and -180 <= lng <= 180):
                continue
            nm = ((row.get(name_field) if name_field else "") or row.get("locality") or "").strip()
            city = _city_for(lat, lng)
            out.append(Store(platform=platform, store_id=sid, name=nm or platform.title(),
                             lat=lat, lng=lng, city=city, locality=_locality_for(city, lat, lng)))
    return out


# ---- intelligent city + sub-area inference from coordinates (offline) --------
# The national CSVs have no usable city/area. We derive both from the place-names
# the data already carries: the zepto CSV's own city column (68 cities) as city
# anchors, and store names that encode an area ("CHN-Sholinganallur", swiggy's
# locality column) as area anchors — then assign every store its nearest.
_AREA_RE = re.compile(r"^[A-Za-z]{2,4}-(.+)$")   # "CHN-Sholinganallur New" -> area
_SNAP_KM = 7.0   # snap stores to the nearest clean locality within this
_MAX_DERIVED_LOCS = 25   # per city: cap the auto-derived snap targets (bounds sprawl)
# Hard cap on how many sub-areas ANY city may expose. Snapping alone still left the
# big metros at ~30 areas, which is an unusable dropdown. Past this cap the smallest
# area is folded into its nearest neighbour ("Koramangala-Indiranagar-HSR Layout")
# until the city fits. 14 rather than 12 keeps the merged labels shorter, since every
# member name is spelled out in full — see _merged_name.
_MAX_LOCALITIES_PER_CITY = 22
_MAX_LABEL_CHARS = 52   # soft budget: keeps a merged name readable on a narrow phone
_ALWAYS_NAME_MIN_STORES = 4   # but an area with this many stores is ALWAYS named in full
_CITY_ALIAS = {
    "bangalore": "Bengaluru", "bengaluru": "Bengaluru", "bombay": "Mumbai",
    "delhi": "Delhi NCR", "new delhi": "Delhi NCR", "gurugram": "Delhi NCR",
    "gurgaon": "Delhi NCR", "noida": "Delhi NCR", "greater noida": "Delhi NCR",
    "ghaziabad": "Delhi NCR", "faridabad": "Delhi NCR", "vadodra": "Vadodara",
}


def _canon_city(c: str) -> str:
    c = (c or "").strip()
    return _CITY_ALIAS.get(c.lower(), c.title()) if c else ""


def _norm_area(a: str) -> str:
    # canonical casing so "ALUVA" (swiggy) and "Aluva" (zepto) are one area,
    # while short all-caps tokens (MP, HSR) and digit-led words survive
    def cap(w):
        if len(w) <= 3 and w.isupper():
            return w
        return (w[0].upper() + w[1:].lower()) if w[:1].isalpha() else w
    return " ".join(cap(w) for w in " ".join((a or "").split()).split())


# strip the qualifiers that fragment ONE locality into many ("HSR Layout Sector 2",
# "HSR Layout - MP", "Koramangala 5th Block" -> "HSR Layout" / "Koramangala")
_CLUB_RE = [
    re.compile(r"\s*\bES\d+\b.*$", re.I),                     # blinkit store code
    re.compile(r"\s+(sector|phase|block|network|stage|extn|extension|gate)\b.*$", re.I),
    re.compile(r"\s*[-–]\s*[A-Za-z0-9]{1,3}$"),               # trailing "- MP"
    re.compile(r"\s+\d+\s*(st|nd|rd|th)?$", re.I),            # trailing number / ordinal
    re.compile(r"\s+(east|west|north|south)$", re.I),         # merge directional halves
    re.compile(r"\s+(new|old)$", re.I),                       # New/Old suffix
]


def _club_area(a: str) -> str:
    out = (a or "").strip()
    for rx in _CLUB_RE:
        out = rx.sub("", out).strip()
    return out or a


def _area_of(name: str, platform: str) -> str | None:
    n = (name or "").strip()
    if not n or n.lower() in ("instamart", platform):
        return None
    m = _AREA_RE.match(n)
    raw = m.group(1).strip() if m else (n if platform == "swiggy" else None)
    return _club_area(_norm_area(raw)) if raw else None


def _cell(lat: float, lng: float):
    return (int(round(lat * 10)), int(round(lng * 10)))   # ~11 km grid


def _enrich_geo(base: dict[str, list[Store]]) -> dict[str, list[Store]]:
    # 1) city anchors from the zepto national CSV (it carries a city column)
    cgrid: dict = {}
    zp = _DS_DIR / "zepto-darkstores.csv"
    if zp.exists():
        with open(zp, newline="", encoding="utf-8", errors="replace") as f:
            for r in csv.DictReader(f):
                cc = _canon_city(r.get("city"))
                try:
                    lat, lng = float(r["lat"]), float(r["lng"])
                except (TypeError, ValueError, KeyError):
                    continue
                if cc:
                    cgrid.setdefault(_cell(lat, lng), []).append((lat, lng, cc))

    def nearest_city(lat, lng, radius=35.0, span=4):
        ci, cj = _cell(lat, lng)
        best, bkm = None, radius
        for i in range(ci - span, ci + span + 1):
            for j in range(cj - span, cj + span + 1):
                for (pl, pn, cc) in cgrid.get((i, j), []):
                    d = haversine_km(lat, lng, pl, pn)
                    if d < bkm:
                        best, bkm = cc, d
        return best

    for stores in base.values():
        for s in stores:
            if not s.city or s.city == "Other":
                nc = nearest_city(s.lat, s.lng)
                if nc:
                    s.city = nc

    # 2) area anchors from stores whose area we can read directly + curated ones
    agrid: dict = {}
    for plat, stores in base.items():
        for s in stores:
            a = _area_of(s.name, plat)
            if a and s.city and s.city != "Other":
                agrid.setdefault(_cell(s.lat, s.lng), []).append((s.lat, s.lng, s.city, a))
    for city, cents in _LOCALITIES.items():
        for area, (cla, clo) in cents.items():
            agrid.setdefault(_cell(cla, clo), []).append((cla, clo, city, area))

    def nearest_area(lat, lng, city, radius=_LOCALITY_RADIUS_KM, span=1):
        ci, cj = _cell(lat, lng)
        best, bkm = None, radius
        for i in range(ci - span, ci + span + 1):
            for j in range(cj - span, cj + span + 1):
                for (pl, pn, c, area) in agrid.get((i, j), []):
                    if c != city:
                        continue
                    d = haversine_km(lat, lng, pl, pn)
                    if d < bkm:
                        best, bkm = area, d
        return best

    # 2.5) derive per-city locality centroids from the biggest clubbed areas, so
    # EVERY city (not just the curated 8) has a dense-but-bounded set of clean
    # snap targets — capped so sprawling metros (Mumbai + Thane/Navi) don't explode.
    acc: dict = {}
    for plat, stores in base.items():
        for s in stores:
            a = _area_of(s.name, plat)
            if not a or not s.city or s.city == "Other":
                continue
            d = acc.setdefault(s.city, {}).setdefault(a, [0.0, 0.0, 0])
            d[0] += s.lat; d[1] += s.lng; d[2] += 1
    derived_locs: dict = {}
    for city, areas in acc.items():
        top = sorted(areas.items(), key=lambda kv: -kv[1][2])[:_MAX_DERIVED_LOCS]
        derived_locs[city] = {a: (v[0] / v[2], v[1] / v[2]) for a, v in top}

    def snap_locality(city, lat, lng):
        best, bkm = None, _SNAP_KM
        for nm, (cla, clo) in _LOCALITIES.get(city, {}).items():   # curated names first
            dkm = haversine_km(lat, lng, cla, clo)
            if dkm < bkm:
                best, bkm = nm, dkm
        for nm, (cla, clo) in derived_locs.get(city, {}).items():
            dkm = haversine_km(lat, lng, cla, clo)
            if dkm < bkm:
                best, bkm = nm, dkm
        return _club_area(best) if best else None   # club curated names too (E/W etc.)

    # 3) snap every store to its nearest clean locality (curated ∪ derived); this
    # is what collapses "HSR Layout Sector 2 / HSR / HSR Layout" into one area and
    # keeps each city's area list short. Fall back to its own clubbed area.
    for plat, stores in base.items():
        for s in stores:
            snap = snap_locality(s.city, s.lat, s.lng) if s.city and s.city != "Other" else None
            if snap:
                s.locality = snap
            else:
                a = _area_of(s.name, plat)
                if a:
                    s.locality = a
                elif not s.locality and s.city and s.city != "Other":
                    na = nearest_area(s.lat, s.lng, s.city)
                    if na:
                        s.locality = na

    # 4) cap each city at _MAX_LOCALITIES_PER_CITY areas. Snapping bounds sprawl but
    # still leaves the metros near 30 — too many to pick from. Merge the geographically
    # closest pair of areas repeatedly (agglomerative, store-count-weighted centroids)
    # until the city fits, so adjacent areas fuse first and far-apart ones stay distinct.
    _apply_locality_cap(base)
    return base


def _merged_name(members: dict) -> str:
    """Spell out EVERY member, biggest first: "Koramangala-Indiranagar-HSR Layout".

    An earlier version truncated to two names plus "+N". That made well-known areas
    unfindable -- HSR Layout vanished inside "JP Nagar-Bannerghatta +5" with nothing
    telling you which entry to pick to reach it. A long label beats a hidden one:
    the whole point of the list is that someone can find their own neighbourhood."""
    ranked = sorted(members.items(), key=lambda kv: (-kv[1], kv[0]))
    ordered = [n for n, _ in ranked]
    label = "-".join(ordered)
    if len(label) <= _MAX_LABEL_CHARS:
        return label
    # Over budget. Never drop an area that has REAL store presence -- hiding "Najafgarh"
    # (8 stores) behind a +N is the same bug as hiding HSR Layout: someone searching for
    # their own neighbourhood cannot tell which entry reaches it. So every member at or
    # above the threshold is named however long that runs, and only the genuinely minor
    # tail (one or two stores, names like "Venice Mall") collapses into the count.
    kept = [n for n, c in ranked if c >= _ALWAYS_NAME_MIN_STORES]
    for n, _ in ranked:                      # then fill the remaining budget, biggest first
        if n in kept:
            continue
        if len("-".join(kept + [n])) > _MAX_LABEL_CHARS:
            continue
        kept.append(n)
    kept = [n for n in ordered if n in kept]  # keep the biggest-first order
    hidden = len(ordered) - len(kept)
    return "-".join(kept) + (f" +{hidden}" if hidden else "")


def _variant_canon(names: dict) -> dict:
    """Map spelling variants of ONE area onto a single canonical name.

    The sources spell the same neighbourhood several ways -- "Hsr" / "HSR Layout",
    "Btm" / "BTM Layout" -- which not only reads as a duplicate inside a merged label
    but can land the SAME place in two different clusters. Treat a shorter normalised
    name that prefixes a longer one as the same area, keeping the busier spelling."""
    norm = {n: re.sub(r"[^a-z0-9]", "", n.lower()) for n in names}
    canon = {}
    for a in names:
        for b in names:
            if a is b:
                continue
            na, nb = norm[a], norm[b]
            # exact match after normalising ("Gillco Park Hills" == "Gillco Parkhills"),
            # or the shorter one prefixes the longer ("Hsr" -> "HSR Layout").
            if len(na) >= 3 and (na == nb or nb.startswith(na)):
                # Prefer the MORE DESCRIPTIVE spelling, not the busier one: which stores
                # end up in the area is identical either way, so the only thing the name
                # decides is whether a human recognises it ("BTM Layout" beats "Btm").
                winner = max((a, b), key=lambda n: (len(n), n))
                canon[a] = winner
                canon[b] = winner
    # resolve chains so every variant lands on one final name
    for k in list(canon):
        seen = set()
        while canon.get(k) and canon[k] != k and canon[k] not in seen:
            seen.add(canon[k])
            k2 = canon[k]
            if canon.get(k2) and canon[k2] != k2:
                canon[k] = canon[k2]
            else:
                break
    return canon


def _apply_locality_cap(base: dict) -> None:
    """Rewrite Store.locality in place so no city exposes more than the cap."""
    # centroid + store count per (city, locality), pooled across platforms so every
    # platform ends up with the SAME area names (the UI switches platform freely).
    agg: dict = {}
    for stores in base.values():
        for s in stores:
            if not s.city or s.city == "Other" or not s.locality:
                continue
            d = agg.setdefault(s.city, {}).setdefault(s.locality, [0.0, 0.0, 0])
            d[0] += s.lat; d[1] += s.lng; d[2] += 1

    # collapse spelling variants BEFORE clustering, so "Hsr" and "HSR Layout" are one
    # area rather than two members of a label (or worse, two separate clusters).
    var_renames: dict = {}
    for city, areas in list(agg.items()):
        counts = {n: v[2] for n, v in areas.items()}
        canon = _variant_canon(counts)
        if not canon:
            continue
        merged: dict = {}
        for name, v in areas.items():
            tgt = canon.get(name, name)
            if tgt != name:
                var_renames[(city, name)] = tgt
            d = merged.setdefault(tgt, [0.0, 0.0, 0])
            d[0] += v[0]; d[1] += v[1]; d[2] += v[2]
        agg[city] = merged
    if var_renames:
        for stores in base.values():
            for s in stores:
                tgt = var_renames.get((s.city, s.locality))
                if tgt:
                    s.locality = tgt

    renames: dict = {}
    for city, areas in agg.items():
        if len(areas) <= _MAX_LOCALITIES_PER_CITY:
            continue
        # each cluster: [lat, lng, count, {member_name: member_count}]
        clusters = [[v[0] / v[2], v[1] / v[2], v[2], {name: v[2]}] for name, v in areas.items()]
        while len(clusters) > _MAX_LOCALITIES_PER_CITY:
            # Absorb the SMALLEST area into its nearest neighbour — never "merge the two
            # closest", which is rich-get-richer: the dense core keeps winning and ends up
            # one giant blob while sparse outskirts each keep a slot. Smallest-first folds
            # thin areas into the recognisable ones next door and keeps sizes even.
            j = min(range(len(clusters)), key=lambda k: (clusters[k][2], clusters[k][0]))
            i = min((k for k in range(len(clusters)) if k != j),
                    key=lambda k: haversine_km(clusters[k][0], clusters[k][1],
                                               clusters[j][0], clusters[j][1]))
            a, b = clusters[i], clusters[j]
            n = a[2] + b[2]
            merged_members = dict(a[3])
            for k, v in b[3].items():
                merged_members[k] = merged_members.get(k, 0) + v
            clusters[i] = [(a[0] * a[2] + b[0] * b[2]) / n,      # count-weighted centroid
                           (a[1] * a[2] + b[1] * b[2]) / n, n, merged_members]
            clusters.pop(j)
        for c in clusters:
            if len(c[3]) > 1:
                label = _merged_name(c[3])
                for member in c[3]:
                    renames[(city, member)] = label

    if renames:
        for stores in base.values():
            for s in stores:
                new = renames.get((s.city, s.locality))
                if new:
                    s.locality = new

    # Final pass: adopt the orphans. A store that never snapped to any anchor keeps
    # locality=None, and since the UI's cascade REQUIRES an area, such a store can
    # never be reached or checked -- it is invisible inventory. Assign each to the
    # nearest final area in its own city so every store in a city is reachable.
    final: dict = {}
    for stores in base.values():
        for s in stores:
            if not s.city or s.city == "Other" or not s.locality:
                continue
            d = final.setdefault(s.city, {}).setdefault(s.locality, [0.0, 0.0, 0])
            d[0] += s.lat; d[1] += s.lng; d[2] += 1
    cents = {c: {n: (v[0] / v[2], v[1] / v[2]) for n, v in areas.items()}
             for c, areas in final.items()}
    for stores in base.values():
        for s in stores:
            if s.locality or not s.city or s.city == "Other":
                continue
            opts = cents.get(s.city)
            if not opts:
                continue
            s.locality = min(opts, key=lambda n: haversine_km(s.lat, s.lng, *opts[n]))


def _load_all() -> dict[str, list[Store]]:
    base = {
        "blinkit": _load_csv(_ROOT / "blinkit.csv", "blinkit",
                             ("merchant_id", "backend_merchant_id"),
                             ("backend_merchant_name", "chain_name")),
        "zepto":   _load_csv(_ROOT / "zepto.csv", "zepto",
                             ("store_id",), ("store_name",)),
        "swiggy":  _load_csv(_ROOT / "swiggy.csv", "swiggy",
                             ("store_id",), ("store_name",)),
    }
    # Merge the darkstoremap.kml export (a peer store source) — add stores whose
    # id we don't already have; existing ids are kept as-is (no coord overwrite).
    for plat, extra in _load_kml(_ROOT / "darkstoremap.kml").items():
        have = {s.store_id for s in base.get(plat, [])}
        for s in extra:
            if s.store_id not in have:
                base[plat].append(s)
                have.add(s.store_id)
    # Merge the national per-platform darkstore CSVs (Online Dark store Data/) —
    # same dedup-by-id policy; this is the big coverage expansion.
    for plat, fn, namef in (("blinkit", "blinkit-darkstores.csv", None),
                            ("zepto", "zepto-darkstores.csv", "name"),
                            ("swiggy", "swiggy-darkstores.csv", "locality")):
        have = {s.store_id for s in base.get(plat, [])}
        for s in _load_ds_csv(_DS_DIR / fn, plat, namef):
            if s.store_id not in have:
                base[plat].append(s)
                have.add(s.store_id)
    return _enrich_geo(base)


_STORES = _load_all()


def nearest(platform: str, lat: float, lng: float):
    best, best_km = None, None
    for s in _STORES.get(platform, []):
        d = haversine_km(lat, lng, s.lat, s.lng)
        if best_km is None or d < best_km:
            best, best_km = s, d
    return best, best_km


def nearest_blinkit(lat: float, lng: float):
    return nearest("blinkit", lat, lng)


def stores_in_city(platform: str, city: str, locality: str | None = None) -> list[Store]:
    out = [s for s in _STORES.get(platform, []) if s.city == city]
    if locality:
        out = [s for s in out if s.locality == locality]
    return out


def cities(platform: str) -> list[str]:
    return sorted({s.city for s in _STORES.get(platform, []) if s.city != "Other"})


def all_cities() -> list[str]:
    seen = set()
    for plat in _STORES:
        seen |= {s.city for s in _STORES[plat] if s.city != "Other"}
    return sorted(seen)


_MAX_LOCS_SHOWN = 30   # cap the area picker to a city's biggest N localities


def localities(city: str) -> list[str]:
    # the city's biggest sub-areas by store count (capped), so the picker stays
    # short and clean instead of listing every one-store hamlet in a sprawl metro
    counts: dict = {}
    for plat in _STORES:
        for s in _STORES[plat]:
            if s.city == city and s.locality:
                counts[s.locality] = counts.get(s.locality, 0) + 1
    top = sorted(counts, key=lambda a: -counts[a])[:_MAX_LOCS_SHOWN]
    return sorted(top)


def counts() -> dict:
    return {p: len(v) for p, v in _STORES.items()}
