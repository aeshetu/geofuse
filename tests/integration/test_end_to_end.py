"""End-to-end: recorded raw pages in S3 -> replay -> PostGIS + Elasticsearch -> API.

Needs the docker compose services (S3 endpoint, PostGIS, Elasticsearch); CI starts them. Run with
    pytest -m integration
"""

import os
from datetime import datetime, timezone

import psycopg
import pytest
from conftest import CONUS, fixture_bytes
from elasticsearch import Elasticsearch
from fastapi.testclient import TestClient

from geofuse import load, pipeline
from geofuse.api import LiveRepository, app, get_repository
from geofuse.config import Settings
from geofuse.sources import RawPage
from geofuse.storage import open_store

pytestmark = pytest.mark.integration
SEED_RUN = "20261101T000000Z"
SINCE = datetime(2026, 11, 1, tzinfo=timezone.utc)
SOURCES = ["usgs_quakes", "wfigs_fires", "mrds_wfs", "ghcn_s3"]


@pytest.fixture(scope="module")
def settings():
    os.environ.setdefault("GEOFUSE_STORE", "s3://geofuse-ci")
    os.environ.setdefault("GEOFUSE_S3_ENDPOINT", "http://localhost:5000")
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    os.environ.setdefault("GEOFUSE_ES_INDEX", "geofuse-ci")
    base = Settings.from_env()
    return Settings(CONUS, base.store_url, base.s3_endpoint, base.pg_dsn, base.es_url, base.es_index)


@pytest.fixture(scope="module")
def loaded(settings):
    store = open_store(settings.store_url, settings.s3_endpoint)
    seeds = {"usgs_quakes": ("quakes.geojson", "application/json"),
             "wfigs_fires": ("wfigs_page.json", "application/json"),
             "mrds_wfs": ("mrds_page.gml", "application/xml"),
             "ghcn_s3": ("ghcnd-stations.txt", "text/plain")}
    for source, (name, ctype) in seeds.items():
        pipeline.land_raw(store, [RawPage(source, 1, f"fixture:{name}", ctype, fixture_bytes(name))], SEED_RUN)

    pg, es = psycopg.connect(settings.pg_dsn), Elasticsearch(settings.es_url)
    pg.execute("DROP TABLE IF EXISTS features, etl_runs")
    pg.commit()
    es.indices.delete(index=settings.es_index, ignore_unavailable=True)
    load.init_db(pg)

    first = pipeline.run(settings, SOURCES, SINCE, store, replay=SEED_RUN, pg_conn=pg, es=es)
    second = pipeline.run(settings, SOURCES, SINCE, store, replay=SEED_RUN, pg_conn=pg, es=es)
    yield first, second, pg, es
    pg.close()


def test_load_is_idempotent(loaded, settings):
    first, second, pg, es = loaded
    assert first["quality"]["valid"] == 14
    assert pg.execute("SELECT count(*) FROM features").fetchone()[0] == 14
    assert es.count(index=settings.es_index)["count"] == 14
    # Re-running the same data updates rows in place instead of duplicating them.
    assert second["loaded"] == {"postgis": 14, "elasticsearch": 14}
    assert pg.execute("SELECT count(DISTINCT last_seen_run) FROM features").fetchone()[0] == 1
    assert pg.execute("SELECT count(*) FROM etl_runs").fetchone()[0] == 2


def test_geometry_is_stored_lon_lat(loaded):
    _, _, pg, _ = loaded
    lon, lat = pg.execute("""SELECT ST_X(geom::geometry), ST_Y(geom::geometry) FROM features
                             WHERE uid = 'mrds_wfs:10098555'""").fetchone()
    assert (round(lon, 5), round(lat, 5)) == (-123.37029, 40.5704)


@pytest.fixture(scope="module")
def client(loaded, settings):
    app.dependency_overrides[get_repository] = lambda: LiveRepository(settings)
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_api_bbox_filter(client):
    r = client.get("/features", params={"bbox": "-124.5,37,-119.5,42", "kind": "mine"}).json()
    assert r["numberReturned"] == 3
    assert {f["properties"]["kind"] for f in r["features"]} == {"mine"}


def test_api_nearby_orders_by_distance(client):
    r = client.get("/nearby", params={"lat": 40.5704, "lon": -123.37029, "radius_km": 100}).json()
    d = [f["properties"]["distance_km"] for f in r["features"]]
    assert d[0] == 0 and d == sorted(d)


def test_api_search_is_typo_tolerant_and_geo_filtered(client):
    hit = client.get("/search", params={"q": "Hapy Go Lucky"}).json()
    assert hit["features"][0]["id"] == "mrds_wfs:10098555"
    far = client.get("/search", params={"q": "Hapy Go Lucky", "lat": 25.0, "lon": -80.0, "radius_km": 50}).json()
    assert far["numberReturned"] == 0


def test_api_health_and_latest_run(client):
    assert client.get("/health").json() == {"postgis": "ok", "elasticsearch": "ok"}
    assert client.get("/runs/latest").json()["replay_of"] == SEED_RUN
