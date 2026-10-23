# Open dive sites

A worldwide list of **named scuba dive sites with coordinates**, built only from openly
licensed sources, ready to ship offline in an app (SQLite) or to load in a GIS (GeoJSON, CSV).

**3,311 dive sites in 89 countries** (build of 2026-10-23), © OpenStreetMap contributors and
Wikidata, under the **ODbL 1.0**. See [data/SUMMARY.md](data/SUMMARY.md) for the full report.

## Files

| File | What |
|---|---|
| `data/divesites.sqlite` | SQLite database (1.25 MB): `site`, `site_source`, `meta`, FTS4 `site_fts` |
| `data/divesites.geojson` | Every site as a GeoJSON point with all fields and sources |
| `data/divesites.csv` | The same as CSV |
| `data/divesites.json.gz` | The same as compressed JSON |
| `data/sources_manifest.json` | Sources, licences, extraction times, OSM database timestamp, excluded sources |
| `data/import_report.json` | Counts: downloaded, rejected by reason, merged, by country and source |
| `data/review_duplicates.csv` | Close pairs kept apart because the match was not certain |
| `data/validation.json` | Results of the automated checks |
| `scripts/` | The pipeline that downloads and rebuilds everything |

## Fields

`id` (stable: the preferred source record, e.g. `osm:n123` or `wikidata:Q42`), `name`,
`alt_names`, `lat`, `lon` (WGS84), `country_code`, `country`, `site_type`, `entry_type`,
`min_depth_m`, `max_depth_m`, `difficulty`, `description`, `license`, and every source record
(`site_source`: source, source id, URL, licence). Only name, position, id and source are always
present; **nothing is invented**: a value appears only when the source states it.

## Sources

| Source | Licence | Used |
|---|---|---|
| [OpenStreetMap](https://www.openstreetmap.org/copyright) via Overpass | ODbL 1.0 | Objects with diving tags that are places to dive (dive centres, shops, schools and clubs are filtered out) |
| [Wikidata](https://www.wikidata.org/) | CC0 1.0 | Items that are dive sites (Q2141554 and its subclasses) |
| [Natural Earth](https://www.naturalearthdata.com/) | Public domain | Country of each site |

Checked and **not used** (licence or data quality): OpenDiveMap (most records copied from a
proprietary catalogue), Dive Vibe (unsourced attributes), Diveboard (non-commercial licence),
commercial catalogues (no licence). Details in `scripts/excluded_sources.json`.

## Rebuild

```sh
pip install rapidfuzz unidecode shapely
cd scripts
python fetch.py --refresh   # downloads (cached, resumable, polite to Overpass)
python build.py             # classify, normalise, deduplicate, write out/
python validate.py          # integrity, coordinates, duplicates, depths, encoding, search, proximity
```

## Improve it

Missing a site? Add it to OpenStreetMap as a point with `name=*` and
`scuba_diving:divespot=yes` (plus `scuba_diving:maxdepth`, `scuba_diving:type`,
`scuba_diving:entry` if you know them); it appears in the next build.

## Licence

Data: [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/), © OpenStreetMap contributors;
Wikidata content CC0. Credit "© OpenStreetMap contributors" when you use it. Scripts: MIT.
See [LICENSE](LICENSE).
