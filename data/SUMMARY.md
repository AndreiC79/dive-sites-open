| Metric | Result |
|---|---|
| Raw records downloaded (unique OSM objects + Wikidata items) | 5,388 |
| Sources successfully imported | osm, wikidata |
| Records rejected (dive centres, unclear, no name, bad coordinates) | 2,017 |
| Duplicates merged | 60 |
| Final unique dive sites | 3,311 |
| Countries represented | 89 |
| SQLite size | 1.25 MB |
| Unresolved possible duplicates (review_duplicates.csv) | 53 |

Sites by source (a site can have several):

| Sources | Sites |
|---|---|
| osm | 2,997 |
| osm+wikidata | 21 |
| wikidata | 293 |

Top countries:

| Country | Sites |
|---|---|
| DE | 433 |
| CH | 345 |
| ID | 310 |
| EG | 278 |
| ZA | 267 |
| NL | 226 |
| FR | 169 |
| PH | 103 |
| TH | 83 |
| AT | 82 |
| MV | 79 |
| CO | 74 |
| ES | 65 |
| US | 56 |
| MT | 53 |
| AU | 52 |
| IT | 46 |
| GB | 44 |
| MY | 40 |
| MH | 34 |
| JP | 31 |
| CA | 26 |
| NO | 18 |
| PL | 18 |
| PW | 18 |

Checks:

- OK   sqlite integrity: PRAGMA integrity_check
- OK   invalid coordinates: none outside WGS84 ranges or at 0,0
- OK   missing names: every site has a name
- OK   exact duplicates: 0 name+position pairs repeated
- OK   missing provenance: 0 sites without a source record
- OK   missing licence: 0 sites without a licence
- OK   implausible depths: 0 sites
- OK   encoding problems: 0 names with replacement characters or mojibake
- OK   dense clusters (info): ['(53.7, 9.13): 42', '(51.48, 10.81): 27']
- OK   search probes (info): [{'search': 'thistlegorm', 'matches': 1, 'within_30km_of_expected': 1, 'examples': ['SS Thistlegorm']}, {'search': 'blue hole', 'matches': 3, 'within_30km_of_expected': 1, 'examples': ['Il-Ħofra tal-Bidwin', 'Blue Holes', 'Blue Hole']}, {'search': 'elphinstone', 'matches': 1, 'within_30km_of_expected': 1, 'examples': ['Elphinstone Reef']}, {'search': 'zenobia', 'matches': 1, 'within_30km_of_expected': 1, 'examples': ['Zenobia Shipwreck']}]
- OK   proximity query: 65 sites within 50 km of Hurghada; box query found 65
- OK   bounding box uses the index: SEARCH site USING INDEX site_lat_lon (lat>? AND lat<?)
