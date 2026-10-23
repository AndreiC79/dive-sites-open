"""
Downloads the raw dive-site records into cache/ (one file per source, with its
extraction time and source version). Re-running reuses what is cached unless
--refresh is given, so an interrupted run resumes where it stopped.

Sources (licences checked 2026-10-23, see README.md):
  osm       OpenStreetMap via the Overpass API          ODbL 1.0
  wikidata  Wikidata via the query service (SPARQL)      CC0 1.0
  countries Natural Earth 1:10m admin-0 countries        public domain (used only to name countries)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
UA = "dive-sites-open/1.0 (https://github.com/AndreiC79/dive-sites-open)"

OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Every tag that marks diving, worldwide. These keys are rare, so one global query per key
# stays small (a few thousand objects); "out center" gives ways and relations a point.
OSM_QUERIES = {
    "divespot": 'nwr["scuba_diving:divespot"];',
    "sport_scuba": 'nwr["sport"~"(^|;)scuba_diving(;|$)"];',
    "sport_key": 'nwr["sport:scuba_diving"];',
    "scuba_type": 'nwr["scuba_diving:type"];',
    "scuba_depth": 'nwr["scuba_diving:maxdepth"]; nwr["scuba_diving:depth"]; nwr["scuba_diving:mindepth"];',
    "scuba_entry": 'nwr["scuba_diving:entry"]; nwr["scuba_diving:entry:boat"]; nwr["scuba_diving:entry:shore"];',
    "scuba_name": 'nwr["scuba_diving:name"]; nwr["scuba_diving:difficulty"];',
    "scuba_plain": 'nwr["scuba_diving"];',
}

WIKIDATA_CLASSES = [
    "Q2141554",   # recreational dive site
    "Q26205080",  # underwater diving site
    "Q41768046",  # inshore dive site
    "Q41768182",  # inland dive site
    "Q41768265",  # shoreline dive site
    "Q41768382",  # offshore dive site
]

NATURAL_EARTH = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
                 "master/geojson/ne_10m_admin_0_countries.geojson")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def http(url: str, data: bytes | None = None, accept: str = "application/json", timeout: int = 600,
         tries: int = 6) -> bytes:
    """GET/POST with retries; 429 and 5xx back off (honouring Retry-After)."""
    delay = 10
    for attempt in range(1, tries + 1):
        req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept": accept})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and attempt < tries:
                wait = int(e.headers.get("Retry-After") or delay)
                log(f"  HTTP {e.code}, retry {attempt}/{tries - 1} in {wait}s")
                time.sleep(wait)
                delay *= 2
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < tries:
                log(f"  {e}, retry {attempt}/{tries - 1} in {delay}s")
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise RuntimeError("unreachable")


def save(name: str, payload: dict) -> None:
    CACHE.mkdir(exist_ok=True)
    tmp = CACHE / (name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CACHE / name)  # atomic: a crash never leaves half a file


def fetch_osm(refresh: bool) -> None:
    for key, body in OSM_QUERIES.items():
        name = f"osm_{key}.json"
        if (CACHE / name).exists() and not refresh:
            log(f"osm {key}: cached")
            continue
        query = f"[out:json][timeout:900];({body});out center tags qt;"
        for endpoint in OVERPASS:
            try:
                log(f"osm {key}: querying {endpoint}")
                raw = json.loads(http(endpoint, data=urllib.parse.urlencode({"data": query}).encode()))
                break
            except Exception as e:  # try the next mirror, then give up on this query only
                log(f"osm {key}: {endpoint} failed: {e}")
                raw = None
        if raw is None:
            log(f"osm {key}: FAILED on every endpoint (recorded, continuing)")
            save(f"osm_{key}.error.json", {"query": query, "failed_at": now()})
            continue
        remark = raw.get("remark")
        if remark:  # Overpass reports timeouts / memory limits here: never keep a truncated result
            log(f"osm {key}: server remark '{remark}' - result discarded")
            save(f"osm_{key}.error.json", {"query": query, "remark": remark, "failed_at": now()})
            continue
        save(name, {
            "source": "osm", "query": query, "extracted_at": now(),
            "osm_base": raw.get("osm3s", {}).get("timestamp_osm_base"),
            "elements": raw["elements"],
        })
        log(f"osm {key}: {len(raw['elements'])} elements")
        time.sleep(5)  # be polite to the public Overpass servers


def fetch_wikidata(refresh: bool) -> None:
    name = "wikidata.json"
    if (CACHE / name).exists() and not refresh:
        log("wikidata: cached")
        return
    values = " ".join("wd:" + c for c in WIKIDATA_CLASSES)
    query = f"""
SELECT ?item ?coord ?cc ?label ?lang ?alt WHERE {{
  VALUES ?cls {{ {values} }}
  ?item wdt:P31 ?cls ; wdt:P625 ?coord .
  OPTIONAL {{ ?item wdt:P17/wdt:P297 ?cc . }}
  OPTIONAL {{ ?item rdfs:label ?label . BIND(LANG(?label) AS ?lang) }}
  OPTIONAL {{ ?item skos:altLabel ?alt . }}
}}"""
    url = "https://query.wikidata.org/sparql?format=json&query=" + urllib.parse.quote(query)
    raw = json.loads(http(url, accept="application/sparql-results+json"))
    rows = raw["results"]["bindings"]
    save(name, {"source": "wikidata", "query": query, "extracted_at": now(), "rows": rows})
    log(f"wikidata: {len(rows)} rows")


def fetch_countries(refresh: bool) -> None:
    name = "ne_10m_admin_0_countries.geojson"
    if (CACHE / name).exists() and not refresh:
        log("natural earth: cached")
        return
    CACHE.mkdir(exist_ok=True)
    data = http(NATURAL_EARTH, accept="application/geo+json")
    (CACHE / name).write_bytes(data)
    log(f"natural earth: {len(data) // 1024} KB")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="download again even when cached")
    ap.add_argument("--only", choices=["osm", "wikidata", "countries"])
    a = ap.parse_args()
    steps = {"countries": fetch_countries, "wikidata": fetch_wikidata, "osm": fetch_osm}
    for k, f in steps.items():
        if a.only in (None, k):
            try:
                f(a.refresh)
            except Exception as e:
                log(f"{k}: FAILED: {e}")
                if a.only:
                    sys.exit(1)
