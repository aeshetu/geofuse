# Requirements and traceability

GeoFuse was scoped from the point of view of one user: a county emergency-operations analyst in
Northern California (a hypothetical persona, written down so every feature has a reason to exist).

> "During an incident I have four browser tabs open: USGS for quakes, the NIFC map for fires, a
> mine database for hazardous sites, and NOAA for weather stations. They use different formats and
> different coordinate conventions, and none of them answers 'what is near this point?'. I need one
> place to ask that, and I need to trust the answer."

## Requirements

| ID | Need (customer's words) | Requirement | Verified by |
|---|---|---|---|
| R1 | "One place to ask" | Ingest 4 sources (USGS FDSN REST API, NIFC ArcGIS FeatureServer, USGS MRDS OGC WFS, NOAA GHCN on S3) into one canonical schema | `test_sources.py` (one parser test per source), `test_pipeline_lands_raw_curated_and_report_then_replays` |
| R2 | "What is near this point?" | `GET /nearby` returns features within a radius, closest first, with distance in km | `test_api_nearby_orders_by_distance` (PostGIS KNN), `scripts/smoke.sh` |
| R3 | "Show me my county" | `GET /features?bbox=` with kind and time filters, paged, as GeoJSON any GIS tool can open | `test_features_returns_geojson_with_lon_lat_order`, `test_api_bbox_filter` |
| R4 | "I don't remember exact names" | `GET /search` with typo-tolerant name matching and an optional radius | `test_api_search_is_typo_tolerant_and_geo_filtered` (Elasticsearch fuzzy match + geo_distance) |
| R5 | "I need to trust the answer" | Validate every record (coordinates, axis order, ids, timestamps); quarantine failures with reasons instead of loading or dropping them silently; publish a data-quality report per run | `test_transform.py` (10 rule cases), `GET /runs/latest` |
| R6 | "It has to be current" | Re-runnable ETL: idempotent upserts keyed on source id, so a scheduled job can run any time without duplicates | `test_load_is_idempotent` |
| R7 | "If a number looks wrong, show me where it came from" | Land raw responses unchanged with SHA-256 manifests; any run can be replayed from storage | `test_pipeline_..._then_replays`, `test_replay_detects_tampered_raw_page` |
| R8 | (IT security / operations) | Containerized, non-root image; deployed only after automated tests pass | `.github/workflows/ci.yml` (unit → integration on the compose stack → publish to GHCR) |

## Decisions the requirements forced

- **Axis order (R5).** The WFS returns zero features, with no error, when the bbox axis order and
  the CRS label disagree. The extractor always sends a URN CRS (lat/lon order), and validation flags
  coordinates that look swapped, so this class of bug cannot reach the database unnoticed.
- **ArcGIS errors (R5).** The FeatureServer reports query errors as HTTP 200 with an `error` body; the
  extractor treats that as a failure instead of "zero fires".
- **Out of scope is not an error (R5).** GHCN ships ~130k stations worldwide; records outside the
  area of interest are counted and dropped, not quarantined, so the quarantine stays meaningful.
- **PostGIS and Elasticsearch (R2–R4).** PostGIS is the system of record and does exact spatial
  math; Elasticsearch serves fuzzy text search. Both are loaded from the same validated batch.
