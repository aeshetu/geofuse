"""The canonical feature record every source is mapped into, plus its validation rules."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from geofuse.config import BBox

KINDS = ("earthquake", "wildfire", "mine", "weather_station")


@dataclass(frozen=True)
class Feature:
    source: str
    source_id: str
    kind: str
    name: str
    lon: float | None
    lat: float | None
    observed_at: datetime | None = None  # UTC; None for static sites (mines, stations)
    attributes: dict = field(default_factory=dict)

    @property
    def uid(self) -> str:
        """Stable key across runs, so loads are idempotent upserts."""
        return f"{self.source}:{self.source_id}"

    def to_record(self) -> dict:
        rec = asdict(self)
        rec["uid"] = self.uid
        rec["observed_at"] = self.observed_at.isoformat() if self.observed_at else None
        return rec

    @classmethod
    def from_record(cls, rec: dict) -> Feature:
        rec = {k: v for k, v in rec.items() if k != "uid"}
        if rec.get("observed_at"):
            rec["observed_at"] = datetime.fromisoformat(rec["observed_at"])
        return cls(**rec)


def utc_from_ms(ms: int | float | None) -> datetime | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


# Rejection reasons. "outside_aoi" is not an error: it marks records a source returned beyond the
# area of interest (GHCN ships the whole planet), which are counted and dropped, not quarantined.
OUTSIDE_AOI = "outside_aoi"


def validate(f: Feature, aoi: BBox, now: datetime | None = None) -> list[str]:
    """Return every rule the record breaks; an empty list means it is loadable."""
    now = now or datetime.now(timezone.utc)
    reasons = []
    if not f.source_id:
        reasons.append("missing_id")
    if f.kind not in KINDS:
        reasons.append("unknown_kind")
    if not (f.name or "").strip():
        reasons.append("missing_name")
    if f.lon is None or f.lat is None or not (math.isfinite(f.lon) and math.isfinite(f.lat)):
        reasons.append("missing_geometry")
    else:
        if not (-180 <= f.lon <= 180 and -90 <= f.lat <= 90):
            # The usual cause is lat/lon swapped somewhere upstream (WFS 1.1 axis order).
            reasons.append("swapped_axes" if (-90 <= f.lon <= 90 and -180 <= f.lat <= 180) else "bad_coordinates")
        elif f.lon == 0 and f.lat == 0:
            reasons.append("null_island")
        elif not aoi.contains(f.lon, f.lat):
            reasons.append(OUTSIDE_AOI)
    if f.observed_at is not None:
        if f.observed_at.tzinfo is None:
            reasons.append("naive_timestamp")
        elif f.observed_at > now + timedelta(days=1):
            reasons.append("future_timestamp")
    return reasons
