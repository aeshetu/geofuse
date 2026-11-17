"""GeoJSON REST API over the loaded data. OpenAPI docs are served at /docs.

    GET /features?bbox=&kind=&since=&limit=&offset=   features in a box (PostGIS)
    GET /nearby?lat=&lon=&radius_km=&kind=            closest features first (PostGIS KNN)
    GET /search?q=&lat=&lon=&radius_km=               fuzzy name search, optional radius (Elasticsearch)
    GET /runs/latest                                  last ETL run report (data quality, counts)
    GET /health                                       dependency checks
"""

from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from typing import Annotated, Literal, Protocol

from fastapi import Depends, FastAPI, HTTPException, Query

from geofuse import __version__
from geofuse.config import BBox, Settings

Kind = Literal["earthquake", "wildfire", "mine", "weather_station"]
MAX_LIMIT = 1000


class Repository(Protocol):
    def in_bbox(self, bbox: BBox, kinds: list[str] | None, since: datetime | None,
                limit: int, offset: int) -> list[dict]: ...
    def nearby(self, lon: float, lat: float, radius_km: float, kinds: list[str] | None,
               limit: int) -> list[dict]: ...
    def search(self, q: str, lon: float | None, lat: float | None, radius_km: float | None,
               limit: int) -> list[dict]: ...
    def latest_run(self) -> dict | None: ...
    def health(self) -> dict: ...


ROW_COLUMNS = "uid, source, kind, name, observed_at, attributes, ST_X(geom::geometry), ST_Y(geom::geometry)"


def _row(r, distance_m=None) -> dict:
    uid, source, kind, name, observed_at, attributes, lon, lat = r[:8]
    rec = {"uid": uid, "source": source, "kind": kind, "name": name, "observed_at": observed_at,
           "attributes": attributes, "lon": lon, "lat": lat}
    if distance_m is not None:
        rec["distance_km"] = round(distance_m / 1000, 3)
    return rec


class LiveRepository:
    """PostGIS for spatial queries, Elasticsearch for text search."""

    def __init__(self, settings: Settings):
        from elasticsearch import Elasticsearch
        from psycopg_pool import ConnectionPool

        self.pool = ConnectionPool(settings.pg_dsn, min_size=1, max_size=5, open=True)
        self.es, self.index = Elasticsearch(settings.es_url), settings.es_index

    def in_bbox(self, bbox, kinds, since, limit, offset):
        sql = f"""SELECT {ROW_COLUMNS} FROM features
                  WHERE geom && ST_MakeEnvelope(%(x0)s, %(y0)s, %(x1)s, %(y1)s, 4326)::geography
                    AND (%(kinds)s::text[] IS NULL OR kind = ANY(%(kinds)s))
                    AND (%(since)s::timestamptz IS NULL OR observed_at >= %(since)s)
                  ORDER BY observed_at DESC NULLS LAST, uid LIMIT %(limit)s OFFSET %(offset)s"""
        params = {"x0": bbox.min_lon, "y0": bbox.min_lat, "x1": bbox.max_lon, "y1": bbox.max_lat,
                  "kinds": kinds, "since": since, "limit": limit, "offset": offset}
        with self.pool.connection() as conn:
            return [_row(r) for r in conn.execute(sql, params).fetchall()]

    def nearby(self, lon, lat, radius_km, kinds, limit):
        sql = f"""WITH p AS (SELECT ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography AS g)
                  SELECT {ROW_COLUMNS}, ST_Distance(geom, p.g) FROM features, p
                  WHERE ST_DWithin(geom, p.g, %(r)s)
                    AND (%(kinds)s::text[] IS NULL OR kind = ANY(%(kinds)s))
                  ORDER BY geom <-> p.g LIMIT %(limit)s"""
        params = {"lon": lon, "lat": lat, "r": radius_km * 1000, "kinds": kinds, "limit": limit}
        with self.pool.connection() as conn:
            return [_row(r, r[8]) for r in conn.execute(sql, params).fetchall()]

    def search(self, q, lon, lat, radius_km, limit):
        query: dict = {"bool": {"must": [{"match": {"name": {"query": q, "fuzziness": "AUTO"}}}]}}
        if lon is not None and lat is not None and radius_km:
            query["bool"]["filter"] = [{"geo_distance": {"distance": f"{radius_km}km",
                                                         "location": {"lat": lat, "lon": lon}}}]
        hits = self.es.search(index=self.index, query=query, size=limit)["hits"]["hits"]
        out = []
        for h in hits:
            s = h["_source"]
            out.append({"uid": s["uid"], "source": s["source"], "kind": s["kind"], "name": s["name"],
                        "observed_at": s.get("observed_at"), "attributes": s.get("attributes", {}),
                        "lon": s["location"]["lon"], "lat": s["location"]["lat"], "score": h["_score"]})
        return out

    def latest_run(self):
        with self.pool.connection() as conn:
            row = conn.execute("SELECT report FROM etl_runs ORDER BY finished_at DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def health(self):
        status = {}
        try:
            with self.pool.connection() as conn:
                conn.execute("SELECT 1")
            status["postgis"] = "ok"
        except Exception as e:  # noqa: BLE001 - health reports any failure
            status["postgis"] = f"error: {e.__class__.__name__}"
        status["elasticsearch"] = "ok" if self.es.ping() else "error"
        return status


@lru_cache(maxsize=1)
def get_repository() -> Repository:
    return LiveRepository(Settings.from_env())


Repo = Annotated[Repository, Depends(get_repository)]
app = FastAPI(title="GeoFuse", version=__version__,
              description="Earthquakes, wildfires, mine sites and weather stations for one area of interest, "
                          "unified from a REST API, an ArcGIS FeatureServer, an OGC WFS and S3.")


def feature_collection(rows: list[dict], **meta) -> dict:
    feats = []
    for r in rows:
        props = {k: v for k, v in r.items() if k not in ("uid", "lon", "lat", "attributes")}
        attrs = r.get("attributes") or {}
        props.update(json.loads(attrs) if isinstance(attrs, str) else attrs)
        if isinstance(props.get("observed_at"), datetime):
            props["observed_at"] = props["observed_at"].isoformat()
        feats.append({"type": "Feature", "id": r["uid"],
                      "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                      "properties": props})
    return {"type": "FeatureCollection", "numberReturned": len(feats), **meta, "features": feats}


@app.get("/features")
def features(repo: Repo,
             bbox: str = Query(..., description="min_lon,min_lat,max_lon,max_lat (EPSG:4326)"),
             kind: Annotated[list[Kind] | None, Query()] = None,
             since: datetime | None = None,
             limit: int = Query(100, ge=1, le=MAX_LIMIT),
             offset: int = Query(0, ge=0)):
    try:
        box = BBox.parse(bbox)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    rows = repo.in_bbox(box, kind, since, limit, offset)
    nxt = offset + limit if len(rows) == limit else None
    return feature_collection(rows, next_offset=nxt)


@app.get("/nearby")
def nearby(repo: Repo,
           lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180),
           radius_km: float = Query(25, gt=0, le=500),
           kind: Annotated[list[Kind] | None, Query()] = None,
           limit: int = Query(50, ge=1, le=MAX_LIMIT)):
    return feature_collection(repo.nearby(lon, lat, radius_km, kind, limit))


@app.get("/search")
def search(repo: Repo, q: str = Query(..., min_length=2),
           lat: float | None = Query(None, ge=-90, le=90), lon: float | None = Query(None, ge=-180, le=180),
           radius_km: float | None = Query(None, gt=0, le=500),
           limit: int = Query(20, ge=1, le=200)):
    if (lat is None) != (lon is None):
        raise HTTPException(status_code=422, detail="lat and lon must be given together")
    return feature_collection(repo.search(q, lon, lat, radius_km, limit))


@app.get("/runs/latest")
def latest_run(repo: Repo):
    report = repo.latest_run()
    if report is None:
        raise HTTPException(status_code=404, detail="no ETL runs recorded yet")
    return report


@app.get("/health")
def health(repo: Repo):
    status = repo.health()
    if any(v != "ok" for v in status.values()):
        raise HTTPException(status_code=503, detail=status)
    return status
