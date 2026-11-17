"""Settings, all overridable through environment variables (12-factor style)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class BBox:
    """Area of interest in lon/lat (EPSG:4326), always stored x-first."""

    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float

    @classmethod
    def parse(cls, text: str) -> BBox:
        parts = [float(p) for p in text.split(",")]
        if len(parts) != 4:
            raise ValueError(f"bbox needs 4 numbers (min_lon,min_lat,max_lon,max_lat), got {text!r}")
        box = cls(*parts)
        if not (-180 <= box.min_lon < box.max_lon <= 180 and -90 <= box.min_lat < box.max_lat <= 90):
            raise ValueError(f"bbox out of range or inverted: {text!r}")
        return box

    def contains(self, lon: float, lat: float) -> bool:
        return self.min_lon <= lon <= self.max_lon and self.min_lat <= lat <= self.max_lat

    def __str__(self) -> str:
        return f"{self.min_lon},{self.min_lat},{self.max_lon},{self.max_lat}"


# Northern California: active faults, a long fire season, and dense mine/quarry records.
DEFAULT_AOI = "-124.5,37.0,-119.5,42.0"


@dataclass(frozen=True)
class Settings:
    aoi: BBox
    store_url: str
    s3_endpoint: str | None
    pg_dsn: str
    es_url: str
    es_index: str

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            aoi=BBox.parse(os.getenv("GEOFUSE_AOI", DEFAULT_AOI)),
            store_url=os.getenv("GEOFUSE_STORE", "file://data/lake"),
            s3_endpoint=os.getenv("GEOFUSE_S3_ENDPOINT") or None,
            pg_dsn=os.getenv("GEOFUSE_PG_DSN", "postgresql://geofuse:geofuse@localhost:5432/geofuse"),
            es_url=os.getenv("GEOFUSE_ES_URL", "http://localhost:9200"),
            es_index=os.getenv("GEOFUSE_ES_INDEX", "geofuse-features"),
        )
