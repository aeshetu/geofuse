from datetime import datetime, timedelta, timezone

import pytest

from geofuse.model import Feature, validate
from geofuse.transform import transform

NOW = datetime(2026, 11, 16, tzinfo=timezone.utc)


def feat(**kw) -> Feature:
    base = dict(source="usgs_quakes", source_id="nc1", kind="earthquake", name="M 2.1 - 5km N of Cobb",
                lon=-122.7, lat=38.8, observed_at=NOW - timedelta(hours=1))
    return Feature(**{**base, **kw})


@pytest.mark.parametrize("kw, reason", [
    ({"lon": None}, "missing_geometry"),
    ({"lon": float("nan")}, "missing_geometry"),
    ({"lon": 38.8, "lat": -122.7}, "swapped_axes"),
    ({"lon": 500.0, "lat": 400.0}, "bad_coordinates"),
    ({"lon": 0.0, "lat": 0.0}, "null_island"),
    ({"name": "  "}, "missing_name"),
    ({"source_id": ""}, "missing_id"),
    ({"kind": "volcano"}, "unknown_kind"),
    ({"observed_at": NOW + timedelta(days=3)}, "future_timestamp"),
    ({"observed_at": datetime(2026, 11, 1)}, "naive_timestamp"),
])
def test_validation_rules(aoi, kw, reason):
    assert reason in validate(feat(**kw), aoi, NOW)


def test_valid_record_has_no_reasons(aoi):
    assert validate(feat(), aoi, NOW) == []


def test_outside_aoi_is_filtered_not_quarantined(aoi):
    result = transform([feat(source_id="far", lon=-77.0, lat=38.9), feat()], aoi, NOW)
    assert result.outside_aoi == 1 and result.rejects == [] and len(result.valid) == 1


def test_rejects_are_quarantined_with_reasons(aoi):
    result = transform([feat(lon=38.8, lat=-122.7)], aoi, NOW)
    assert result.valid == []
    assert result.rejects[0]["reasons"] == ["swapped_axes"]
    assert result.rejects[0]["record"]["uid"] == "usgs_quakes:nc1"
    assert result.reasons["swapped_axes"] == 1


def test_duplicates_keep_most_recent_observation(aoi):
    old = feat(name="old", observed_at=NOW - timedelta(days=2))
    new = feat(name="new", observed_at=NOW - timedelta(hours=1))
    result = transform([new, old], aoi, NOW)
    assert result.duplicates == 1 and [f.name for f in result.valid] == ["new"]


def test_record_round_trip():
    f = feat(attributes={"magnitude": 2.1})
    assert Feature.from_record(f.to_record()) == f
