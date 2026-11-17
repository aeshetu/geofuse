"""Current wildfire incidents from the NIFC WFIGS ArcGIS FeatureServer, paged by resultOffset.

ArcGIS REST quirks handled here: errors come back as HTTP 200 with an "error" body, and the server
signals more pages with exceededTransferLimit rather than a total count.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime

from geofuse.config import BBox
from geofuse.http import TIMEOUT
from geofuse.model import Feature, utc_from_ms
from geofuse.sources import RawPage, SourceError

NAME = "wfigs_fires"
URL = ("https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
       "WFIGS_Incident_Locations_Current/FeatureServer/0/query")
PAGE_SIZE = 1000  # layer maxRecordCount is 2000
FIELDS = ["OBJECTID", "IrwinID", "IncidentName", "IncidentTypeCategory", "FireDiscoveryDateTime",
          "ModifiedOnDateTime_dt", "IncidentSize", "PercentContained", "POOState", "POOCounty"]


def _check(doc: dict) -> dict:
    if "error" in doc:
        err = doc["error"]
        raise SourceError(f"{NAME}: ArcGIS error {err.get('code')}: {err.get('message')} {err.get('details')}")
    return doc


def fetch(session, aoi: BBox, since: datetime, page_size: int = PAGE_SIZE, **_) -> Iterator[RawPage]:
    # "Current" incidents are already a live view, so `since` is not applied server-side.
    offset, seq = 0, 0
    while True:
        params = {
            "where": "1=1", "outFields": ",".join(FIELDS), "orderByFields": "OBJECTID",
            "geometry": str(aoi), "geometryType": "esriGeometryEnvelope", "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects", "outSR": 4326,
            "resultOffset": offset, "resultRecordCount": page_size, "f": "json",
        }
        resp = session.get(URL, params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        doc = _check(json.loads(resp.content))
        seq += 1
        yield RawPage(NAME, seq, resp.url, "application/json", resp.content)
        n = len(doc.get("features", []))
        if not doc.get("exceededTransferLimit") or n == 0:
            return
        offset += n


def parse(page: RawPage) -> Iterator[Feature]:
    doc = _check(json.loads(page.body))
    for f in doc.get("features", []):
        a = f.get("attributes") or {}
        g = f.get("geometry") or {}
        irwin = (a.get("IrwinID") or "").strip("{}").lower()
        yield Feature(
            source=NAME,
            source_id=irwin or str(a.get("OBJECTID") or ""),
            kind="wildfire",
            name=(a.get("IncidentName") or "").strip().title(),
            lon=g.get("x"),
            lat=g.get("y"),
            observed_at=utc_from_ms(a.get("FireDiscoveryDateTime")),
            attributes={
                "category": a.get("IncidentTypeCategory"),  # WF = wildfire, RX = prescribed burn
                "size_acres": a.get("IncidentSize"),
                "percent_contained": a.get("PercentContained"),
                "state": (a.get("POOState") or "").removeprefix("US-") or None,
                "county": a.get("POOCounty"),
                "modified_at": (utc_from_ms(a.get("ModifiedOnDateTime_dt")).isoformat()
                                if a.get("ModifiedOnDateTime_dt") else None),
            },
        )
