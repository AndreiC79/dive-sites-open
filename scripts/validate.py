"""
Checks the built catalogue (out/divesites.sqlite) and writes out/validation.json plus the
summary table in out/SUMMARY.md. Exits non-zero when a hard check fails.
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"


def haversine_m(a_lat, a_lon, b_lat, b_lon):
    r = 6371008.8
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # site names in every script
    db =sqlite3.connect(OUT / "divesites.sqlite")
    rep = json.loads((OUT / "import_report.json").read_text(encoding="utf-8"))
    checks, fails = {}, []

    def check(name, ok, detail):
        checks[name] = {"ok": bool(ok), "detail": detail}
        if not ok:
            fails.append(name)

    check("sqlite integrity", db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "PRAGMA integrity_check")
    n = db.execute("SELECT COUNT(*) FROM site").fetchone()[0]
    check("invalid coordinates", db.execute("SELECT COUNT(*) FROM site WHERE lat NOT BETWEEN -90 AND 90 OR lon NOT BETWEEN -180 AND 180 OR (lat = 0 AND lon = 0)").fetchone()[0] == 0, "none outside WGS84 ranges or at 0,0")
    check("missing names", db.execute("SELECT COUNT(*) FROM site WHERE TRIM(name) = ''").fetchone()[0] == 0, "every site has a name")
    exact = db.execute("SELECT COUNT(*) FROM (SELECT LOWER(name), ROUND(lat, 5), ROUND(lon, 5) FROM site GROUP BY 1, 2, 3 HAVING COUNT(*) > 1)").fetchone()[0]
    check("exact duplicates", exact == 0, f"{exact} name+position pairs repeated")
    no_src = db.execute("SELECT COUNT(*) FROM site s WHERE NOT EXISTS (SELECT 1 FROM site_source x WHERE x.site_id = s.id)").fetchone()[0]
    check("missing provenance", no_src == 0, f"{no_src} sites without a source record")
    no_lic = db.execute("SELECT COUNT(*) FROM site WHERE license IS NULL OR license = ''").fetchone()[0]
    check("missing licence", no_lic == 0, f"{no_lic} sites without a licence")
    bad_depth = db.execute("SELECT COUNT(*) FROM site WHERE max_depth_m <= 0 OR max_depth_m > 350 OR min_depth_m > max_depth_m").fetchone()[0]
    check("implausible depths", bad_depth == 0, f"{bad_depth} sites")
    enc = db.execute("SELECT COUNT(*) FROM site WHERE name LIKE '%�%' OR name LIKE '%Ã%' OR name LIKE '%â€%'").fetchone()[0]
    check("encoding problems", enc == 0, f"{enc} names with replacement characters or mojibake")

    # Clusters: many sites on one 0.01° cell usually means a bulk import mistake.
    cells = Counter((round(la, 2), round(lo, 2)) for la, lo in db.execute("SELECT lat, lon FROM site"))
    dense = [(k, v) for k, v in cells.most_common(5) if v >= 15]
    checks["dense clusters (info)"] = {"ok": True, "detail": [f"{k}: {v}" for k, v in dense] or "none with 15+ sites per 0.01°"}

    # Search and proximity on known places (only checked when present).
    probes = []
    for word, near in [("thistlegorm", (27.81, 33.92)), ("blue hole", (28.57, 34.54)), ("elphinstone", (25.31, 34.86)), ("zenobia", (34.9, 33.65))]:
        rows = db.execute("SELECT s.name, s.lat, s.lon FROM site s WHERE s.rowid IN (SELECT docid FROM site_fts WHERE site_fts MATCH ?)",
                          (" ".join(w + "*" for w in word.split()),)).fetchall()
        close = [r for r in rows if haversine_m(near[0], near[1], r[1], r[2]) < 30000]
        probes.append({"search": word, "matches": len(rows), "within_30km_of_expected": len(close), "examples": [r[0] for r in rows[:3]]})
    checks["search probes (info)"] = {"ok": True, "detail": probes}
    # Bounding-box proximity agrees with a full scan.
    lat0, lon0, r = 27.26, 33.81, 50000.0
    dlat = r / 111195.0
    dlon = dlat / math.cos(math.radians(lat0 + dlat))
    boxed = {i for i, la, lo in db.execute("SELECT id, lat, lon FROM site WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
                                          (lat0 - dlat, lat0 + dlat, lon0 - dlon, lon0 + dlon)) if haversine_m(lat0, lon0, la, lo) <= r}
    full = {i for i, la, lo in db.execute("SELECT id, lat, lon FROM site") if haversine_m(lat0, lon0, la, lo) <= r}
    check("proximity query", boxed == full, f"{len(full)} sites within 50 km of Hurghada; box query found {len(boxed)}")
    plan = " ".join(str(x[-1]) for x in db.execute("EXPLAIN QUERY PLAN SELECT id FROM site WHERE lat BETWEEN 1 AND 2 AND lon BETWEEN 3 AND 4"))
    check("bounding box uses the index", "site_lat_lon" in plan, plan)

    countries = db.execute("SELECT COUNT(DISTINCT country_code) FROM site WHERE country_code IS NOT NULL").fetchone()[0]
    size = (OUT / "divesites.sqlite").stat().st_size
    rejected = sum(rep.get("osm_rejected", {}).values()) + sum(rep.get("wikidata_rejected", {}).values())
    raw = rep["osm_unique_objects"] + rep["wikidata_items"]
    summary = [
        ("Raw records downloaded (unique OSM objects + Wikidata items)", f"{raw:,}"),
        ("Sources successfully imported", ", ".join(k for k, v in rep["accepted_source_records"].items() if v)),
        ("Records rejected (dive centres, unclear, no name, bad coordinates)", f"{rejected:,}"),
        ("Duplicates merged", f"{rep['duplicates_merged']:,}"),
        ("Final unique dive sites", f"{n:,}"),
        ("Countries represented", f"{countries}"),
        ("SQLite size", f"{size / 1024 / 1024:.2f} MB"),
        ("Unresolved possible duplicates (review_duplicates.csv)", f"{rep['unresolved_possible_duplicates']:,}"),
    ]
    by_source = rep["sites_only_from"]
    top = list(rep["by_country"].items())[:25]
    md = ["| Metric | Result |", "|---|---|"] + [f"| {a} | {b} |" for a, b in summary]
    md += ["", "Sites by source (a site can have several):", "", "| Sources | Sites |", "|---|---|"] + [f"| {k} | {v:,} |" for k, v in by_source.items()]
    md += ["", "Top countries:", "", "| Country | Sites |", "|---|---|"] + [f"| {k} | {v:,} |" for k, v in top]
    md += ["", "Checks:", ""] + [f"- {'OK  ' if v['ok'] else 'FAIL'} {k}: {v['detail']}" for k, v in checks.items()]
    (OUT / "SUMMARY.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (OUT / "validation.json").write_text(json.dumps({"checks": checks, "summary": dict(summary)}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n".join(md))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
