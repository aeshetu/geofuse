"""USGS earthquakes from the FDSN Event web service (REST API, GeoJSON), paged by offset."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime

from geofuse.config import BBox
from geofuse.http import TIMEOUT
from geofuse.model import Feature, utc_from_ms
from geofuse.sources import RawPage, SourceError

NAME = "usgs_quakes"
URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
PAGE_SIZE = 1000  # the service caps a request at 20,000; smaller pages fail and retry cheaply


def fetch(session, aoi: BBox, since: datetime, page_size: int = PAGE_SIZE, **_) -> Iterator[RawPage]:
    offset, seq = 1, 0  # FDSN offsets are 1-based
    while True:
        params = {
            "format": "geojson", "orderby": "time-asc", "starttime": since.strftime("%Y-%m-%dT%H:%M:%S"),
            "minlongitude": aoi.min_lon, "minlatitude": aoi.min_lat,
            "maxlongitude": aoi.max_lon, "maxlatitude": aoi.max_lat,
            "limit": page_size, "offset": offset,
        }
        resp = session.get(URL, params=params, timeout=TIMEOUT)
        if resp.status_code == 204:  # FDSN's "no events" answer
            return
        resp.raise_for_status()
        seq += 1
        yield RawPage(NAME, seq, resp.url, "application/json", resp.content)
        n = len(json.loads(resp.content).get("features", []))
        if n < page_size:
            return
        offset += n


def parse(page: RawPage) -> Iterator[Feature]:
    doc = json.loads(page.body)
    if doc.get("type") != "FeatureCollection":
        raise SourceError(f"{NAME}: expected a FeatureCollection, got {str(doc)[:200]}")
    for f in doc["features"]:
        p = f.get("properties") or {}
        coords = (f.get("geometry") or {}).get("coordinates") or [None, None, None]
        yield Feature(
            source=NAME,
            source_id=f.get("id") or "",
            kind="earthquake",
            name=p.get("title") or p.get("place") or "",
            lon=coords[0],
            lat=coords[1],
            observed_at=utc_from_ms(p.get("time")),
            attributes={
                "magnitude": p.get("mag"), "mag_type": p.get("magType"),
                "depth_km": coords[2] if len(coords) > 2 else None,
                "place": p.get("place"), "status": p.get("status"), "alert": p.get("alert"),
                "tsunami": p.get("tsunami"), "felt": p.get("felt"), "significance": p.get("sig"),
                "updated_at": utc_from_ms(p.get("updated")).isoformat() if p.get("updated") else None,
                "url": p.get("url"),
            },
        )
