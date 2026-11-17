"""USGS Mineral Resources Data System sites from an OGC Web Feature Service (WFS 1.1.0, GML 3).

Axis order is the trap here. In WFS 1.1.0, "EPSG:4326" means latitude first, while the server's
un-suffixed bbox means longitude first; mixing them returns zero features without any error. We
always send the CRS as a URN with lat/lon order and read gml:pos back as "lat lon".

Paging needs a stable sort. Without sortBy, startIndex pages overlap: on the Northern California AOI
the run fetched 590 sites twice and silently missed 590 others (2.1%). Found because the run
report's duplicate count was non-zero; fixed by sorting on the record id.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterator
from datetime import datetime

from geofuse.config import BBox
from geofuse.http import TIMEOUT
from geofuse.model import Feature
from geofuse.sources import RawPage, SourceError

NAME = "mrds_wfs"
URL = "https://mrdata.usgs.gov/services/wfs/mrds"
PAGE_SIZE = 5000
NS = {
    "gml": "http://www.opengis.net/gml",
    "wfs": "http://www.opengis.net/wfs",
    "ms": "http://mapserver.gis.umn.edu/mapserver",
    "ows": "http://www.opengis.net/ows",
}
CRS = "urn:ogc:def:crs:EPSG::4326"


def bbox_param(aoi: BBox) -> str:
    """WFS 1.1.0 bbox in the URN's lat/lon axis order."""
    return f"{aoi.min_lat},{aoi.min_lon},{aoi.max_lat},{aoi.max_lon},{CRS}"


def _root(body: bytes) -> ET.Element:
    root = ET.fromstring(body)
    if root.tag.endswith("ExceptionReport"):
        text = " ".join(t.strip() for t in root.itertext() if t.strip())
        raise SourceError(f"{NAME}: WFS exception: {text[:300]}")
    return root


def fetch(session, aoi: BBox, since: datetime, page_size: int = PAGE_SIZE, **_) -> Iterator[RawPage]:
    # Mine sites are static records; every run takes a full snapshot of the AOI.
    start, seq = 0, 0
    while True:
        params = {
            "service": "WFS", "version": "1.1.0", "request": "GetFeature", "typeName": "mrds",
            "bbox": bbox_param(aoi), "maxFeatures": page_size, "startIndex": start,
            "sortBy": "dep_id",  # stable order, or pages overlap and records go missing
        }
        resp = session.get(URL, params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        n = len(_root(resp.content).findall("gml:featureMember", NS))
        seq += 1
        yield RawPage(NAME, seq, resp.url, "application/xml", resp.content)
        if n < page_size:
            return
        start += n


def parse(page: RawPage) -> Iterator[Feature]:
    for member in _root(page.body).findall("gml:featureMember", NS):
        site = member.find("ms:mrds", NS)
        if site is None:
            continue

        def text(tag: str, site=site) -> str | None:
            el = site.find(f"ms:{tag}", NS)
            return el.text.strip() if el is not None and el.text and el.text.strip() else None

        lat = lon = None
        pos = site.find(".//gml:Point/gml:pos", NS)
        if pos is not None and pos.text:
            lat, lon = (float(v) for v in pos.text.split()[:2])  # URN CRS => lat first
        commodities = text("code_list")
        yield Feature(
            source=NAME,
            source_id=text("dep_id") or site.get(f"{{{NS['gml']}}}id", ""),
            kind="mine",
            name=text("site_name") or "",
            lon=lon,
            lat=lat,
            attributes={
                "development_status": text("dev_stat"),
                "commodities": commodities.split() if commodities else [],
                "county_fips": (text("fips_code") or "").removeprefix("f") or None,
                "url": text("url"),
            },
        )
