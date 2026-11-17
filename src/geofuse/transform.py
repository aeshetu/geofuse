"""Validate, filter to the AOI, and de-duplicate parsed features."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from geofuse.config import BBox
from geofuse.model import OUTSIDE_AOI, Feature, validate


@dataclass
class TransformResult:
    valid: list[Feature] = field(default_factory=list)
    rejects: list[dict] = field(default_factory=list)  # quarantined records with their reasons
    outside_aoi: int = 0
    duplicates: int = 0
    reasons: Counter = field(default_factory=Counter)


def transform(features: Iterable[Feature], aoi: BBox, now: datetime | None = None) -> TransformResult:
    now = now or datetime.now(timezone.utc)
    out = TransformResult()
    latest: dict[str, Feature] = {}
    for f in features:
        reasons = validate(f, aoi, now)
        if reasons == [OUTSIDE_AOI]:
            out.outside_aoi += 1
            continue
        if reasons:
            out.reasons.update(reasons)
            out.rejects.append({"reasons": reasons, "record": f.to_record()})
            continue
        prev = latest.get(f.uid)
        if prev is not None:
            out.duplicates += 1
            # Keep the most recent observation of the same upstream record.
            if (f.observed_at or now) < (prev.observed_at or now):
                continue
        latest[f.uid] = f
    out.valid = list(latest.values())
    return out
