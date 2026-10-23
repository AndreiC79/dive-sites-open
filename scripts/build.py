"""
Builds the dive-site catalogue from cache/ (run fetch.py first):

  out/divesites.sqlite      the database the app ships (assets/divesites.sqlite)
  out/divesites.csv, .geojson, .json.gz
  out/sources_manifest.json  every source: licence, attribution, extraction time, version
  out/import_report.json     counts, rejections by reason, merges, coverage
  out/review_duplicates.csv  close pairs kept apart because the match was not certain
  out/raw_records.json.gz    every accepted source record as downloaded (audit trail)

Nothing is invented: a field the source does not give stays empty.
"""
from __future__ import annotations

import csv
import gzip
import json
import math
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from rapidfuzz import fuzz
from shapely.geometry import Point, shape
from shapely.strtree import STRtree
from unidecode import unidecode

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
OUT = HERE / "out"
SCHEMA_VERSION = 1

LICENCES = {
    "osm": {
        "name": "OpenStreetMap", "license": "ODbL-1.0",
        "license_url": "https://opendatacommons.org/licenses/odbl/1-0/",
        "attribution": "© OpenStreetMap contributors", "attribution_url": "https://www.openstreetmap.org/copyright",
        "url": "https://www.openstreetmap.org/",
    },
    "wikidata": {
        "name": "Wikidata", "license": "CC0-1.0",
        "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
        "attribution": "Wikidata (CC0)", "attribution_url": "https://www.wikidata.org/",
        "url": "https://www.wikidata.org/",
    },
}

# ---------------------------------------------------------------- OSM: site or centre?

# Present on an OSM object = a business (dive centre, shop, school, club, hotel...), not a place to dive.
CENTRE_KEYS = {"shop", "office", "club", "craft", "healthcare", "opening_hours", "phone", "email",
               "contact:phone", "contact:email", "contact:website", "website", "brand", "operator:type"}
CENTRE_SCUBA_KEYS = ("courses", "rental", "air_fill", "filling", "nitrox", "trimix", "oxygen", "repair",
                     "education", "organization", "equipment", "spoken_languages", "guided_tours", "store")
CENTRE_AMENITY = {"dive_centre", "school", "college", "university", "restaurant", "cafe", "bar", "pub",
                  "community_centre", "shelter", "parking", "toilets"}
CENTRE_LEISURE = {"sports_centre", "swimming_pool", "fitness_centre", "water_park", "sports_hall",
                  "fitness_station", "resort", "marina", "slipway"}
CENTRE_TOURISM = {"hotel", "guest_house", "hostel", "motel", "apartment", "chalet", "camp_site", "resort",
                  "information", "attraction_shop"}
CENTRE_BUILDING = True  # any building=* is a structure on land

# A name that is a business's: dive centres, schools, clubs, shops, in the languages mappers use.
BUSINESS_NAME = re.compile(
    r"\b(divers$|dive ?(cent(er|re)|shop|school|club|resort|base|team|tours?|academy|station|world|zone|adventures?|services?)"
    r"|diving (cent(er|re)|school|club|resort|base|team|tours?|academy|adventures?|services?|company|world)"
    r"|scuba (cent(er|re)|school|club|shop|tours?|world)|tauch(schule|basis|club|center|sport|zentrum)"
    r"|(club|centre|ecole) de plongee|(centro|escuela|club) de buceo|centro immersioni|duik(school|centrum|vereniging)"
    r"|centrum nurkowe|klub nurkowy|dykker(senter|klubb|skole)|academy|padi|naui|resort|hotel|ltd|gmbh|inc|sarl)\b")

# Present = something about the underwater place itself.
SITE_SCUBA_KEYS = ("type", "maxdepth", "depth", "mindepth", "entry", "difficulty", "current", "dangers",
                   "descent_line", "access_easyness", "name", "visibility")
SITE_NATURAL = {"reef", "bay", "cape", "rock", "bare_rock", "cave_entrance", "water", "spring", "strait",
                "shoal", "beach", "cliff", "arch", "sinkhole", "coastline", "islet"}


def osm_class(t: dict) -> str:
    """'site', 'centre' or 'unclear' for an OSM tag set."""
    divespot = t.get("scuba_diving:divespot")
    if divespot == "no":
        return "centre"
    scuba = {k.split(":", 1)[1] for k in t if k.startswith("scuba_diving:")}
    centre = (any(k in t for k in CENTRE_KEYS)
              or t.get("amenity") in CENTRE_AMENITY
              or t.get("leisure") in CENTRE_LEISURE
              or t.get("tourism") in CENTRE_TOURISM
              or ("building" in t and CENTRE_BUILDING)
              or any(s.startswith(CENTRE_SCUBA_KEYS) for s in scuba))
    strong_centre = ("shop" in t or t.get("amenity") == "dive_centre" or "office" in t
                     or any(s.startswith(("courses", "rental", "air_fill", "filling", "nitrox_filling")) for s in scuba))
    site = (divespot == "yes"
            or any(s.startswith(SITE_SCUBA_KEYS) for s in scuba)
            or t.get("natural") in SITE_NATURAL
            or t.get("historic") == "wreck"
            or t.get("seamark:type") in ("wreck", "rock", "obstruction")
            or t.get("place") in ("islet", "island")
            or t.get("man_made") in ("pier", "breakwater", "groyne"))
    if divespot == "yes" and not strong_centre:
        return "site"
    if centre or t.get("scuba_diving:diveshop") == "yes":
        return "centre"
    if site or divespot:  # divespot=wreck/reef/... are sites too
        return "site"
    # Only sport=scuba_diving and a name: a mapped dive site, unless the name is a business's.
    name = " ".join(v for k, v in t.items() if k == "name" or k.startswith("name:"))
    if not name:
        return "unclear"
    return "centre" if BUSINESS_NAME.search(unidecode(name).lower()) else "site"


# ---------------------------------------------------------------- parsing helpers

NUM = re.compile(r"(\d+(?:[.,]\d+)?)")


def depth_m(value: str | None) -> tuple[float | None, float | None]:
    """'25', '25 m', '18-30', '80 ft', '5 - 40m' -> (min, max) metres; nonsense -> (None, None)."""
    if not value:
        return None, None
    v = value.lower()
    nums = [float(n.replace(",", ".")) for n in NUM.findall(v)]
    if not nums:
        return None, None
    feet = "ft" in v or "feet" in v or "'" in v
    nums = [round(n * 0.3048, 1) if feet else n for n in nums]
    if any(n > 350 or n < 0 for n in nums):  # deeper than any recreational/technical site: a typo
        return None, None
    nums = [n for n in nums if n > 0]  # "0" says nothing about a dive site
    if not nums:
        return None, None
    return (min(nums), max(nums)) if len(nums) > 1 else (None, nums[0])


def entry_of(t: dict) -> str | None:
    e = t.get("scuba_diving:entry")
    if e:
        return e.split(";")[0].strip().lower() or None
    kinds = [k.split(":")[-1] for k in t if k.startswith("scuba_diving:entry:") and t[k] == "yes"]
    return ";".join(sorted(kinds)) or None


def type_of(t: dict) -> str | None:
    v = t.get("scuba_diving:type")
    kinds = [v.lower()] if v else []
    kinds += [k.split(":")[-1] for k in t if k.startswith("scuba_diving:type:") and t[k] == "yes"]
    if t.get("historic") == "wreck" or t.get("seamark:type") == "wreck":
        kinds.append("wreck")
    if t.get("natural") == "reef":
        kinds.append("reef")
    if t.get("natural") in ("cave_entrance", "sinkhole"):
        kinds.append("cave")
    if t.get("natural") == "water" or t.get("water") in ("lake", "reservoir", "pond", "quarry"):
        kinds.append("lake")
    out = []
    for k in kinds:
        for part in re.split(r"[;,]", k):
            p = part.strip()
            if p and p not in out:
                out.append(p)
    return ";".join(out) or None


NAME_KEYS_ALT = ("alt_name", "old_name", "official_name", "local_name", "short_name", "loc_name", "int_name")


def names_of(t: dict) -> tuple[str | None, list[str]]:
    primary = t.get("name") or t.get("scuba_diving:name") or t.get("name:en")
    if not primary:
        for k, v in t.items():
            if k.startswith("name:"):
                primary = v
                break
    alts = []
    for k, v in t.items():
        if (k.startswith("name:") or k in NAME_KEYS_ALT or k == "scuba_diving:name") and v:
            for part in v.split(";"):
                p = part.strip()
                if p and p != primary and p not in alts:
                    alts.append(p)
    return (primary.strip() if primary else None), alts


def norm(name: str) -> str:
    s = unidecode(name).lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def native(name: str) -> str:
    """Words in their own script, lower case, accents dropped (what the app searches with)."""
    s = unicodedata.normalize("NFD", name)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn").lower()
    return re.sub(r"[^\w]+|_", " ", s).strip()


def haversine_m(a_lat, a_lon, b_lat, b_lon) -> float:
    r = 6371008.8
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


# ---------------------------------------------------------------- loading

def load_osm(report: dict) -> list[dict]:
    by_key: dict[str, dict] = {}
    meta = []
    for f in sorted(CACHE.glob("osm_*.json")):
        if f.name.endswith(".error.json"):
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        meta.append({"file": f.name, "extracted_at": d["extracted_at"], "osm_base": d.get("osm_base"),
                     "elements": len(d["elements"])})
        for e in d["elements"]:
            by_key[f"{e['type'][0]}{e['id']}"] = e  # the same object comes from several queries
    report["osm_files"] = meta
    report["osm_failed_queries"] = sorted(f.name for f in CACHE.glob("osm_*.error.json"))
    rejected = Counter()
    out = []
    for key, e in by_key.items():
        t = e.get("tags", {})
        lat = e.get("lat", e.get("center", {}).get("lat"))
        lon = e.get("lon", e.get("center", {}).get("lon"))
        cls = osm_class(t)
        if cls != "site":
            rejected["dive centre / business" if cls == "centre" else "not clearly a dive site"] += 1
            continue
        name, alts = names_of(t)
        if not name:
            rejected["no name"] += 1
            continue
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            rejected["no or invalid coordinates"] += 1
            continue
        lo, hi = depth_m(t.get("scuba_diving:maxdepth") or t.get("scuba_diving:depth"))
        mn, _ = depth_m(t.get("scuba_diving:mindepth"))
        typ = {"n": "node", "w": "way", "r": "relation"}[key[0]]
        out.append({
            "key": f"osm:{key}", "source": "osm", "source_id": f"{typ}/{e['id']}",
            "source_url": f"https://www.openstreetmap.org/{typ}/{e['id']}",
            "name": name, "alt_names": alts, "lat": round(lat, 7), "lon": round(lon, 7),
            "site_type": type_of(t), "entry_type": entry_of(t),
            "min_depth_m": mn if mn is not None else lo, "max_depth_m": hi,
            "difficulty": t.get("scuba_diving:difficulty"),
            "description": t.get("description") or t.get("description:en"),
            "country_code_src": (t.get("addr:country") or "").upper() or None,
            "wikidata": t.get("wikidata"), "divespot": t.get("scuba_diving:divespot") == "yes",
            "is_node": key[0] == "n", "raw": e,
        })
    report["osm_unique_objects"] = len(by_key)
    report["osm_rejected"] = dict(rejected)
    return out


def load_wikidata(report: dict) -> list[dict]:
    d = json.loads((CACHE / "wikidata.json").read_text(encoding="utf-8"))
    items: dict[str, dict] = {}
    for r in d["rows"]:
        q = r["item"]["value"].rsplit("/", 1)[1]
        it = items.setdefault(q, {"labels": {}, "alts": set(), "coords": set(), "cc": set()})
        if "label" in r:
            it["labels"][r.get("lang", {}).get("value", "")] = r["label"]["value"]
        if "alt" in r:
            it["alts"].add(r["alt"]["value"])
        m = re.match(r"Point\(([-\d.eE]+) ([-\d.eE]+)\)", r["coord"]["value"])
        if m:
            it["coords"].add((float(m.group(2)), float(m.group(1))))
        if "cc" in r:
            it["cc"].add(r["cc"]["value"])
    rejected = Counter()
    out = []
    for q, it in items.items():
        name = it["labels"].get("en") or next(iter(it["labels"].values()), None)
        if not name or name == q:
            rejected["no name"] += 1
            continue
        if len(it["coords"]) != 1:
            # Several coordinates on one item: which is the site is unknown, so it is left out.
            rejected["several or no coordinates"] += 1
            continue
        lat, lon = next(iter(it["coords"]))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            rejected["no or invalid coordinates"] += 1
            continue
        alts = sorted({v for v in it["labels"].values() if v != name} | {a for a in it["alts"] if a != name})
        out.append({
            "key": f"wikidata:{q}", "source": "wikidata", "source_id": q,
            "source_url": f"https://www.wikidata.org/wiki/{q}",
            "name": name, "alt_names": alts, "lat": round(lat, 7), "lon": round(lon, 7),
            "site_type": None, "entry_type": None, "min_depth_m": None, "max_depth_m": None,
            "difficulty": None, "description": None,
            "country_code_src": next(iter(it["cc"])) if len(it["cc"]) == 1 else None,
            "wikidata": q, "divespot": True, "is_node": True,
            "raw": {"item": q, "labels": it["labels"], "alts": sorted(it["alts"]), "coords": sorted(it["coords"]),
                    "cc": sorted(it["cc"])},
        })
    report["wikidata_items"] = len(items)
    report["wikidata_rejected"] = dict(rejected)
    report["wikidata_extracted_at"] = d["extracted_at"]
    return out


# ---------------------------------------------------------------- countries

class Countries:
    def __init__(self):
        gj = json.loads((CACHE / "ne_10m_admin_0_countries.geojson").read_text(encoding="utf-8"))
        self.geoms, self.codes, self.names = [], [], []
        for f in gj["features"]:
            p = f["properties"]
            code = p.get("ISO_A2_EH") or p.get("ISO_A2")
            if not code or code == "-99":
                continue
            self.geoms.append(shape(f["geometry"]))
            self.codes.append(code)
            self.names.append(p.get("NAME_EN") or p.get("ADMIN"))
        self.tree = STRtree(self.geoms)

    def at(self, lat: float, lon: float) -> tuple[str | None, str | None]:
        """The country containing the point; offshore, the nearest within ~200 km when the next
        nearest country is clearly farther (a reef between two countries stays unknown)."""
        pt = Point(lon, lat)
        for i in self.tree.query(pt, predicate="intersects"):
            return self.codes[i], self.names[i]
        near = self.tree.query(pt.buffer(2.0))  # degrees; ≈200 km
        if len(near) == 0:
            return None, None
        best: dict[str, float] = {}
        for i in near:
            d = self.geoms[i].distance(pt)
            best[self.codes[i]] = min(d, best.get(self.codes[i], 99.0))
        ranked = sorted(best.items(), key=lambda kv: kv[1])
        code, d1 = ranked[0]
        if d1 > 1.8:
            return None, None
        if len(ranked) > 1 and ranked[1][1] < d1 * 1.5 and d1 > 0.25:
            return None, None
        return code, self.names[self.codes.index(code)]


# ---------------------------------------------------------------- deduplication

PRIORITY = {"osm": 0, "wikidata": 1}


def similarity(a: dict, b: dict) -> float:
    na = [norm(a["name"])] + [norm(x) for x in a["alt_names"]]
    nb = [norm(b["name"])] + [norm(x) for x in b["alt_names"]]
    best = 0.0
    for x in na:
        for y in nb:
            if x and y:
                best = max(best, fuzz.token_sort_ratio(x, y), fuzz.ratio(x, y))
    return best


DIRECTIONS = {"n", "s", "e", "w", "ne", "nw", "se", "sw", "north", "south", "east", "west", "northern", "southern",
              "eastern", "western", "nord", "sud", "est", "ouest", "norte", "sur", "este", "oeste", "nofsinhar", "tramuntana",
              "inner", "outer", "upper", "lower", "bow", "stern", "big", "small", "little", "deep", "shallow", "left", "right",
              "i", "ii", "iii", "iv", "v", "a", "b", "c"}


def conflicting(x: str, y: str) -> bool:
    """Names that differ in a number or a direction/part word are different sites ('Zandeiland 8'
    vs 'Zandeiland 9', 'Qawra North' vs 'Qawra South', 'bow' vs 'stern')."""
    tx, ty = set(norm(x).split()), set(norm(y).split())
    digits = lambda t: {w for w in t if any(c.isdigit() for c in w)}
    return digits(tx) != digits(ty) or (tx & DIRECTIONS) != (ty & DIRECTIONS)


def pair(a, b, d, s) -> dict:
    return {"a": a["key"], "a_name": a["name"], "b": b["key"], "b_name": b["name"],
            "distance_m": round(d), "name_similarity": round(s)}


def dedupe(recs: list[dict], report: dict) -> tuple[list[list[dict]], list[dict]]:
    """Union-find over close pairs. The radius depends on how alike the names are, so two
    different sites a few metres apart stay apart; unsure pairs go to the review list."""
    parent = list(range(len(recs)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    cell = 0.03  # ≈3 km
    grid = defaultdict(list)
    for i, r in enumerate(recs):
        grid[(int(r["lat"] // cell), int(r["lon"] // cell))].append(i)
    review, merges = [], Counter()
    seen = set()
    for (gy, gx), idx in grid.items():
        near = [j for dy in (-1, 0, 1) for dx in (-1, 0, 1) for j in grid.get((gy + dy, gx + dx), [])]
        for i in idx:
            for j in near:
                if j <= i or (i, j) in seen:
                    continue
                seen.add((i, j))
                a, b = recs[i], recs[j]
                d = haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
                if d > 3000:
                    continue
                exact = norm(a["name"]) == norm(b["name"])
                if a["source"] == b["source"]:
                    # One source maps distinct places on purpose; only the same name drawn twice
                    # (a point and an area, or two points a few metres apart) is one site.
                    twice = exact and (d <= 30 or (a["is_node"] != b["is_node"] and d <= 300))
                    if twice:
                        parent[find(i)] = find(j)
                        merges["same source, drawn twice"] += 1
                    elif exact and d <= 1000:
                        review.append(pair(a, b, d, 100))
                    continue
                linked = bool(a["wikidata"]) and a["wikidata"] == b["wikidata"]
                s = similarity(a, b)
                if not linked and conflicting(a["name"], b["name"]):
                    if d <= 1500 and s >= 70:
                        review.append(pair(a, b, d, s))
                    continue
                if linked or (d <= 150 and s >= 80) or (d <= 1000 and s >= 90) or (d <= 3000 and exact):
                    parent[find(i)] = find(j)
                    merges["wikidata link" if linked else "same name" if exact else "similar name"] += 1
                elif d <= 75 or (d <= 1500 and s >= 70):
                    review.append(pair(a, b, d, s))
    groups = defaultdict(list)
    for i, r in enumerate(recs):
        groups[find(i)].append(r)
    report["merge_reasons"] = dict(merges)
    # A review pair already merged through other members is no longer open.
    key_group = {r["key"]: find(i) for i, r in enumerate(recs)}
    review = [p for p in review if key_group[p["a"]] != key_group[p["b"]]]
    return list(groups.values()), review


def canonical(group: list[dict]) -> dict:
    g = sorted(group, key=lambda r: (PRIORITY[r["source"]], not r["divespot"], not r["is_node"], r["key"]))
    best = g[0]
    out = {k: best[k] for k in ("name", "lat", "lon")}
    out["id"] = best["key"]
    for f in ("site_type", "entry_type", "min_depth_m", "max_depth_m", "difficulty", "description", "country_code_src"):
        out[f] = next((r[f] for r in g if r[f] not in (None, "")), None)
    alts = []
    for r in g:
        for n in [r["name"]] + r["alt_names"]:
            if n != out["name"] and n not in alts:
                alts.append(n)
    out["alt_names"] = alts
    out["sources"] = [{"source": r["source"], "source_id": r["source_id"], "source_url": r["source_url"],
                       "license": LICENCES[r["source"]]["license"]} for r in g]
    out["members"] = [r["key"] for r in g]
    return out


# ---------------------------------------------------------------- outputs

def write_sqlite(sites: list[dict], path: Path, built_at: str) -> None:
    if path.exists():
        path.unlink()
    db = sqlite3.connect(path)
    db.executescript("""
    PRAGMA page_size = 4096;
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE site (
      id TEXT PRIMARY KEY NOT NULL,       -- stable: the preferred source record, e.g. osm:n123 or wikidata:Q42
      name TEXT NOT NULL,
      alt_names TEXT,                     -- names joined by ' | '
      search TEXT NOT NULL,               -- name + alt names, transliterated and lower-case
      lat REAL NOT NULL,
      lon REAL NOT NULL,
      country_code TEXT, country TEXT, region TEXT, locality TEXT,
      site_type TEXT, entry_type TEXT, min_depth_m REAL, max_depth_m REAL, difficulty TEXT, description TEXT,
      license TEXT NOT NULL,
      last_updated TEXT NOT NULL
    );
    CREATE INDEX site_lat_lon ON site (lat, lon);
    CREATE INDEX site_country ON site (country_code);
    CREATE TABLE site_source (
      site_id TEXT NOT NULL, source TEXT NOT NULL, source_id TEXT NOT NULL, source_url TEXT, license TEXT NOT NULL,
      PRIMARY KEY (site_id, source, source_id)
    );
    CREATE VIRTUAL TABLE site_fts USING fts4(content="site", search);
    """)
    rows = []
    for s in sites:
        lic = "ODbL-1.0" if any(x["source"] == "osm" for x in s["sources"]) else "CC0-1.0"
        # Each name transliterated (Râs → ras) and as written in its own script (lower case),
        # so a search in either form matches; duplicates removed to keep the index small.
        words = []
        for n in [s["name"]] + s["alt_names"]:
            for w in (norm(n) + " " + native(n)).split():
                if w not in words:
                    words.append(w)
        search = " ".join(words)
        rows.append((s["id"], s["name"], " | ".join(s["alt_names"]) or None, search, s["lat"], s["lon"],
                     s["country_code"], s["country"], None, None, s["site_type"], s["entry_type"],
                     s["min_depth_m"], s["max_depth_m"], s["difficulty"], s["description"], lic, built_at))
    db.executemany("INSERT INTO site VALUES (" + ",".join("?" * 18) + ")", rows)
    db.executemany("INSERT INTO site_source VALUES (?,?,?,?,?)",
                   [(s["id"], x["source"], x["source_id"], x["source_url"], x["license"]) for s in sites for x in s["sources"]])
    db.execute("INSERT INTO site_fts(site_fts) VALUES('rebuild')")
    meta = {
        "schema_version": str(SCHEMA_VERSION), "built_at": built_at, "site_count": str(len(sites)),
        "license": "ODbL-1.0",
        "attribution": "Dive site data © OpenStreetMap contributors (ODbL 1.0) and Wikidata (CC0)",
        "attribution_url": "https://www.openstreetmap.org/copyright",
        "license_url": "https://opendatacommons.org/licenses/odbl/1-0/",
    }
    db.executemany("INSERT INTO meta VALUES (?,?)", list(meta.items()))
    db.commit()
    db.execute("VACUUM")
    db.close()


def main() -> None:
    OUT.mkdir(exist_ok=True)
    built_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report: dict = {"built_at": built_at}
    osm = load_osm(report)
    wd = load_wikidata(report)
    recs = osm + wd
    report["accepted_source_records"] = {"osm": len(osm), "wikidata": len(wd)}

    # Exact duplicates (same source record twice) cannot happen: keys are unique per source.
    groups, review = dedupe(recs, report)
    countries = Countries()
    sites = []
    for g in groups:
        s = canonical(g)
        cc, cname = countries.at(s["lat"], s["lon"])
        if cc is None and s["country_code_src"]:
            cc = s["country_code_src"]
        s["country_code"], s["country"] = cc, cname
        sites.append(s)
    sites.sort(key=lambda s: s["id"])
    report["duplicates_merged"] = len(recs) - len(groups)
    report["final_sites"] = len(sites)
    report["groups_over_2_members"] = sum(1 for g in groups if len(g) > 2)
    report["unresolved_possible_duplicates"] = len(review)
    report["by_source"] = dict(Counter(x["source"] for s in sites for x in s["sources"]))
    report["sites_only_from"] = dict(Counter("+".join(sorted({x["source"] for x in s["sources"]})) for s in sites))
    report["by_country"] = dict(Counter(s["country_code"] or "unresolved" for s in sites).most_common())
    report["countries_represented"] = len({s["country_code"] for s in sites if s["country_code"]})
    report["with_depth"] = sum(1 for s in sites if s["max_depth_m"] is not None)
    report["with_type"] = sum(1 for s in sites if s["site_type"])

    sqlite_path = OUT / "divesites.sqlite"
    write_sqlite(sites, sqlite_path, built_at)
    report["sqlite_bytes"] = sqlite_path.stat().st_size

    cols = ["id", "name", "alt_names", "lat", "lon", "country_code", "country", "site_type", "entry_type",
            "min_depth_m", "max_depth_m", "difficulty", "description", "sources"]
    with open(OUT / "divesites.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for s in sites:
            w.writerow([" | ".join(s[c]) if c == "alt_names" else
                        " ".join(x["source"] + ":" + x["source_id"] for x in s[c]) if c == "sources" else s[c] for c in cols])
    clean = [{k: v for k, v in s.items() if k not in ("members", "country_code_src")} for s in sites]
    (OUT / "divesites.geojson").write_text(json.dumps({
        "type": "FeatureCollection",
        "license": "ODbL-1.0", "attribution": "© OpenStreetMap contributors; Wikidata (CC0)",
        "features": [{"type": "Feature", "id": s["id"], "geometry": {"type": "Point", "coordinates": [s["lon"], s["lat"]]},
                      "properties": {k: v for k, v in s.items() if k not in ("lat", "lon")}} for s in clean],
    }, ensure_ascii=False), encoding="utf-8")
    with gzip.open(OUT / "divesites.json.gz", "wt", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False)
    with gzip.open(OUT / "raw_records.json.gz", "wt", encoding="utf-8") as f:
        json.dump([{"key": r["key"], "raw": r["raw"]} for r in recs], f, ensure_ascii=False)
    with open(OUT / "review_duplicates.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["a", "a_name", "b", "b_name", "distance_m", "name_similarity"])
        w.writeheader()
        w.writerows(sorted(review, key=lambda p: p["distance_m"]))

    manifest = {"built_at": built_at, "database_license": "ODbL-1.0", "sources": []}
    for k, lic in LICENCES.items():
        manifest["sources"].append({**lic, "id": k, "records_accepted": report["accepted_source_records"][k],
                                    "extracted": report["osm_files"] if k == "osm" else report["wikidata_extracted_at"]})
    manifest["helper_data"] = [{"name": "Natural Earth 1:10m admin-0 countries", "license": "Public domain",
                                "url": "https://www.naturalearthdata.com/", "use": "country of each site"}]
    manifest["excluded"] = json.loads((HERE / "excluded_sources.json").read_text(encoding="utf-8"))
    (OUT / "sources_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "import_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("accepted_source_records", "duplicates_merged", "final_sites",
                                             "countries_represented", "unresolved_possible_duplicates", "sqlite_bytes",
                                             "osm_rejected", "wikidata_rejected", "merge_reasons")}, indent=1))


if __name__ == "__main__":
    main()
