from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from geofuse.api import app, get_repository

ROW = {"uid": "usgs_quakes:nc1", "source": "usgs_quakes", "kind": "earthquake", "name": "M 2.1 - Cobb",
       "observed_at": datetime(2026, 11, 15, tzinfo=timezone.utc), "attributes": {"magnitude": 2.1},
       "lon": -122.7, "lat": 38.8}


class FakeRepo:
    def __init__(self):
        self.calls = []

    def in_bbox(self, bbox, kinds, since, limit, offset):
        self.calls.append(("bbox", bbox, kinds, since, limit, offset))
        return [ROW] * min(limit, 3)

    def nearby(self, lon, lat, radius_km, kinds, limit):
        self.calls.append(("nearby", lon, lat, radius_km, kinds, limit))
        return [{**ROW, "distance_km": 1.234}]

    def search(self, q, lon, lat, radius_km, limit):
        self.calls.append(("search", q, lon, lat, radius_km, limit))
        return [{**ROW, "score": 3.2}]

    def latest_run(self):
        return {"run_id": "20261116T000000Z", "quality": {"valid": 1}}

    def health(self):
        return {"postgis": "ok", "elasticsearch": "ok"}


@pytest.fixture
def client_repo():
    repo = FakeRepo()
    app.dependency_overrides[get_repository] = lambda: repo
    yield TestClient(app), repo
    app.dependency_overrides.clear()


def test_features_returns_geojson_with_lon_lat_order(client_repo):
    client, repo = client_repo
    r = client.get("/features", params={"bbox": "-124.5,37,-119.5,42", "kind": ["earthquake", "wildfire"]})
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "FeatureCollection" and body["numberReturned"] == 3
    f = body["features"][0]
    assert f["geometry"] == {"type": "Point", "coordinates": [-122.7, 38.8]}
    assert f["properties"]["magnitude"] == 2.1 and f["properties"]["observed_at"].startswith("2026-11-15")
    assert repo.calls[0][2] == ["earthquake", "wildfire"]


def test_features_paging_sets_next_offset(client_repo):
    client, _ = client_repo
    assert client.get("/features", params={"bbox": "-124,37,-120,42", "limit": 3}).json()["next_offset"] == 3
    assert client.get("/features", params={"bbox": "-124,37,-120,42", "limit": 10}).json()["next_offset"] is None


@pytest.mark.parametrize("params", [
    {"bbox": "1,2,3"},                       # too few numbers
    {"bbox": "-119,42,-124,37"},             # inverted
    {"bbox": "-124,37,-120,42", "limit": 5000},
    {"bbox": "-124,37,-120,42", "kind": "volcano"},
])
def test_features_rejects_bad_input(client_repo, params):
    client, _ = client_repo
    assert client.get("/features", params=params).status_code == 422


def test_nearby_passes_radius_and_returns_distance(client_repo):
    client, repo = client_repo
    r = client.get("/nearby", params={"lat": 38.8, "lon": -122.7, "radius_km": 10})
    assert r.json()["features"][0]["properties"]["distance_km"] == 1.234
    assert repo.calls[0][:4] == ("nearby", -122.7, 38.8, 10)


def test_search_requires_lat_and_lon_together(client_repo):
    client, _ = client_repo
    assert client.get("/search", params={"q": "cobb", "lat": 38.8}).status_code == 422
    assert client.get("/search", params={"q": "cobb", "lat": 38.8, "lon": -122.7}).status_code == 200


def test_runs_and_health(client_repo):
    client, _ = client_repo
    assert client.get("/runs/latest").json()["run_id"] == "20261116T000000Z"
    assert client.get("/health").json() == {"postgis": "ok", "elasticsearch": "ok"}


def test_openapi_documents_every_endpoint(client_repo):
    client, _ = client_repo
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/features", "/nearby", "/search", "/runs/latest", "/health"} <= set(paths)
