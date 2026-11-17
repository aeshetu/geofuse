"""NOAA GHCN-Daily weather stations, read from the public noaa-ghcn-pds S3 bucket with the AWS SDK.

The station list is one fixed-width text file covering the whole planet; it is clipped to the AOI
during validation (counted as out of scope, not as errors).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

from geofuse.config import BBox
from geofuse.model import Feature
from geofuse.sources import RawPage

NAME = "ghcn_s3"
BUCKET = "noaa-ghcn-pds"
KEY = "ghcnd-stations.txt"

# Column positions from NOAA's ghcnd readme (1-based inclusive there, 0-based half-open here).
COLUMNS = {
    "id": (0, 11), "lat": (12, 20), "lon": (21, 30), "elevation": (31, 37), "state": (38, 40),
    "name": (41, 71), "gsn": (72, 75), "hcn_crn": (76, 79), "wmo_id": (80, 85),
}


def public_s3_client():
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config

    return boto3.client("s3", region_name="us-east-1", config=Config(signature_version=UNSIGNED))


def fetch(session, aoi: BBox, since: datetime, s3_client=None, bucket: str = BUCKET, **_) -> Iterator[RawPage]:
    client = s3_client or public_s3_client()
    obj = client.get_object(Bucket=bucket, Key=KEY)
    yield RawPage(NAME, 1, f"s3://{bucket}/{KEY}", "text/plain", obj["Body"].read())


def _field(line: str, name: str) -> str:
    a, b = COLUMNS[name]
    return line[a:b].strip()


def parse(page: RawPage) -> Iterator[Feature]:
    for line in page.body.decode("ascii", errors="replace").splitlines():
        if not line.strip():
            continue
        elevation = _field(line, "elevation")
        yield Feature(
            source=NAME,
            source_id=_field(line, "id"),
            kind="weather_station",
            name=_field(line, "name").title(),
            lon=float(_field(line, "lon")),
            lat=float(_field(line, "lat")),
            attributes={
                # -999.9 is NOAA's missing-elevation sentinel
                "elevation_m": None if elevation in ("", "-999.9") else float(elevation),
                "state": _field(line, "state") or None,
                "gsn": _field(line, "gsn") == "GSN",
                "network": _field(line, "hcn_crn") or None,
                "wmo_id": _field(line, "wmo_id") or None,
            },
        )
