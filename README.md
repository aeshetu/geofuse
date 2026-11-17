# GeoFuse

**Multi-source geospatial ETL: a REST API, an ArcGIS FeatureServer, an OGC WFS and an S3 bucket,
validated into one schema, loaded into PostGIS and Elasticsearch, and served as a GeoJSON API.**

Built for one user's question: *"What earthquakes, wildfires, mine sites and weather stations are
near this point, and can I trust the answer?"* ([requirements and traceability](docs/requirements.md)).

```
                   EXTRACT                          LAND (S3)                TRANSFORM               LOAD / SERVE
 USGS FDSN ── REST API, GeoJSON, offset paging ─┐
 NIFC WFIGS ─ ArcGIS FeatureServer, resultOffset ┤   raw/<source>/dt=/run=    validate 10 rules     PostGIS  (geography + GiST)
 USGS MRDS ── OGC WFS 1.1.0, GML 3, startIndex ──┼─► page-NNNN.gz + manifest ─► quarantine failures ─► Elasticsearch (geo_point,
 NOAA GHCN ── public S3 bucket via boto3 SDK ────┘   (sha256, replayable)      clip to AOI, dedupe       fuzzy text)
                                                     curated/ · quarantine/                         FastAPI: /features /nearby
                                                     runs/<run_id>.json                              /search /runs/latest /health
```

## Results

Live run on 2026-11-17, Northern California area of interest, 30-day look-back for events:

| Source | Interface | Pages | Records parsed | Valid in AOI |
|---|---|---|---|---|
| USGS earthquakes | REST API (FDSN), GeoJSON | 2 | 1,220 | 1,220 |
| NIFC wildfire incidents | ArcGIS FeatureServer, Esri JSON | 1 | 24 | 24 |
| USGS mine and quarry sites | OGC WFS 1.1.0, GML 3 | 6 | 28,444 | 28,444 |
| NOAA weather stations | S3 (boto3), fixed-width text | 1 | 133,041 | 1,988 |
| **Total** | | **10** | **162,729** | **31,676** |

The run took about 37 s on a laptop. 131,053 stations outside the area were filtered out, 0 records failed
validation, and 0 duplicates remained after the WFS paging fix below. The pipeline has also run
against an S3 endpoint (moto server), and replaying that run from its stored raw pages gave identical counts.
CI runs 42 unit tests and 6 end-to-end tests.

## What it demonstrates

| Area | How |
|---|---|
| **Data interfaces (ETL/API)** | Four extractors, each with its paging protocol (FDSN 1-based `offset`, ArcGIS `resultOffset` + `exceededTransferLimit`, WFS `startIndex`, S3 `GetObject`), shared retry/backoff on 429/5xx, and service-specific error detection (ArcGIS errors inside HTTP 200, WFS `ExceptionReport`). The output side is a documented REST API (OpenAPI at `/docs`) with input validation and paging. |
| **Storage, access, exchange** | AWS S3 through boto3 (anonymous reads from NOAA's bucket; the lake writes to any S3 endpoint), ArcGIS FeatureServer, OGC WFS, PostGIS, Elasticsearch. |
| **Data structures** | One canonical `Feature` record; a PostGIS table with `geography(Point)`, a GiST index, a `CHECK` constraint and JSONB attributes; a strict Elasticsearch mapping (`geo_point`, `flattened`, keyword subfields); a partitioned data-lake layout. |
| **Data evaluation** | 10 validation rules (missing or swapped coordinates, null island, out-of-range, future or timezone-less timestamps, unknown kinds, missing ids/names). Failures are quarantined with reasons, and every run writes a data-quality report. |
| **Reliability** | Raw responses are landed untouched with SHA-256 manifests, so any run can be replayed offline and tampering is detected. Loads are idempotent upserts (COPY into a staging table, then one `INSERT … ON CONFLICT`), so re-runs never duplicate. |
| **CI/CD and containers** | GitHub Actions: lint + unit tests → build the image, start the Docker Compose stack (S3 API, PostGIS, Elasticsearch), run end-to-end tests, smoke-test the running API container over HTTP → push to GitHub Container Registry only if all of that passed. Non-root image, health checks. |

## Lessons from real data

- **Unsorted WFS paging loses data without any error.** The first live run reported 584 duplicate mine ids.
  Comparing against one unpaged request showed that `startIndex` pages without a sort order overlap:
  590 sites were fetched twice and **590 others (2.1%) never arrived**. The total row count still
  matched, so only the uniqueness check caught it. Adding `sortBy=dep_id` fixed it, and a regression test
  keeps it fixed.
- **WFS axis order silently returns nothing.** In WFS 1.1.0, `bbox=…,EPSG:4326` means latitude first,
  while the same bbox without a CRS means longitude first on this server. Get it wrong and the
  service answers with a valid, empty collection. GeoFuse always sends a URN CRS with explicit lat/lon
  order, a unit test pins the parameter, and validation flags coordinates that look swapped.
- **ArcGIS reports errors with HTTP 200.** A bad query returns `{"error": …}` with a success status, which
  naive code reads as "no fires". The extractor raises instead.
- **Out of scope is not invalid.** NOAA's station file covers the whole planet. Counting out-of-area
  records separately keeps the quarantine limited to real data problems.

## Run it

```bash
# Windows: .\scripts\dev.ps1     Linux/macOS: scripts/dev.sh
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q                                   # unit tests, no network or databases
geofuse run --no-load                       # live extract into ./data/lake, prints a quality summary

# Full stack (Docker)
docker compose up -d --wait                 # S3 API, PostGIS, Elasticsearch, API on :8000
docker compose run --rm etl                 # init schema + live ETL into the stack
open http://localhost:8000/docs
curl "localhost:8000/nearby?lat=38.79&lon=-122.76&radius_km=20&kind=earthquake"
```

Settings are environment variables: `GEOFUSE_AOI` (default Northern California
`-124.5,37,-119.5,42`), `GEOFUSE_STORE` (`file://…` or `s3://bucket/prefix`), `GEOFUSE_S3_ENDPOINT`,
`GEOFUSE_PG_DSN`, `GEOFUSE_ES_URL`, `GEOFUSE_ES_INDEX`.

## Layout

```
src/geofuse/sources/   one module per upstream service (fetch raw pages, parse to Features)
src/geofuse/model.py   canonical record + validation rules
src/geofuse/storage.py S3 / local object store, lake layout
src/geofuse/transform.py, load.py, pipeline.py, api.py
src/geofuse/schema.sql, es_mapping.json
tests/                 unit tests (recorded fixtures, moto for S3); tests/integration runs on the compose stack
scripts/               dev.ps1, dev.sh, smoke.sh
docs/requirements.md   customer needs → requirements → tests
```

## Data sources

All public, no keys: [USGS Earthquake Catalog API](https://earthquake.usgs.gov/fdsnws/event/1/),
[NIFC WFIGS current incident locations](https://data-nifc.opendata.arcgis.com/),
[USGS Mineral Resources Data System WFS](https://mrdata.usgs.gov/),
[NOAA GHCN-Daily on AWS Open Data](https://registry.opendata.aws/noaa-ghcn/).
