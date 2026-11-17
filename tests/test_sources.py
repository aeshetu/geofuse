import json
import re
from datetime import datetime, timezone

import boto3
import pytest
from conftest import FakeSession, fixture_bytes
from moto import mock_aws

from geofuse.sources import RawPage, SourceError, ghcn_s3, mrds_wfs, usgs_quakes, wfigs_fires

SINCE = datetime(2026, 11, 1, tzinfo=timezone.utc)


def page(mod, body: bytes, ctype="application/json") -> RawPage:
    return RawPage(mod.NAME, 1, "fixture", ctype, body)


# --- USGS FDSN (REST API, GeoJSON) -------------------------------------------------------------

def test_usgs_parse_maps_geojson_to_canonical_fields():
    feats = list(usgs_quakes.parse(page(usgs_quakes, fixture_bytes("quakes.geojson"))))
    assert len(feats) == 3
    f = feats[0]
    assert f.kind == "earthquake" and f.source_id.startswith("nc")
    assert -124.5 <= f.lon <= -119.5 and 37 <= f.lat <= 42
    assert f.observed_at.tzinfo is timezone.utc
    assert f.attributes["magnitude"] is not None and f.attributes["depth_km"] is not None


def test_usgs_fetch_pages_with_1_based_offset_until_short_page(aoi):
    full = json.dumps({"type": "FeatureCollection", "features": [{}, {}]}).encode()
    short = json.dumps({"type": "FeatureCollection", "features": [{}]}).encode()
    session = FakeSession([full, short])
    pages = list(usgs_quakes.fetch(session, aoi, SINCE, page_size=2))
    assert len(pages) == 2
    assert [c["offset"] for c in session.calls] == [1, 3]
    assert session.calls[0]["minlongitude"] == -124.5 and session.calls[0]["maxlatitude"] == 42.0


def test_usgs_fetch_treats_204_as_no_events(aoi):
    assert list(usgs_quakes.fetch(FakeSession([(204, b"")]), aoi, SINCE)) == []


# --- NIFC WFIGS (ArcGIS FeatureServer) ---------------------------------------------------------

def test_wfigs_parse_normalizes_ids_names_and_state():
    feats = list(wfigs_fires.parse(page(wfigs_fires, fixture_bytes("wfigs_page.json"))))
    assert len(feats) == 3
    f = feats[0]
    assert f.kind == "wildfire"
    assert f.source_id == f.source_id.lower() and "{" not in f.source_id  # IrwinID braces stripped
    assert f.name == f.name.title()
    assert f.attributes["state"] and not f.attributes["state"].startswith("US-")


def test_wfigs_fetch_follows_exceeded_transfer_limit(aoi):
    more = json.dumps({"exceededTransferLimit": True, "features": [{}, {}]}).encode()
    last = json.dumps({"features": [{}]}).encode()
    session = FakeSession([more, last])
    assert len(list(wfigs_fires.fetch(session, aoi, SINCE, page_size=2))) == 2
    assert [c["resultOffset"] for c in session.calls] == [0, 2]
    assert session.calls[0]["geometry"] == str(aoi) and session.calls[0]["outSR"] == 4326


def test_wfigs_error_inside_http_200_raises(aoi):
    err = json.dumps({"error": {"code": 400, "message": "Invalid query", "details": []}}).encode()
    with pytest.raises(SourceError, match="Invalid query"):
        list(wfigs_fires.fetch(FakeSession([err]), aoi, SINCE))


# --- USGS MRDS (OGC WFS 1.1.0, GML) ------------------------------------------------------------

def test_wfs_bbox_uses_urn_lat_lon_axis_order(aoi):
    assert mrds_wfs.bbox_param(aoi) == "37.0,-124.5,42.0,-119.5,urn:ogc:def:crs:EPSG::4326"


def test_wfs_parse_reads_gml_pos_as_lat_lon():
    feats = list(mrds_wfs.parse(page(mrds_wfs, fixture_bytes("mrds_page.gml"), "application/xml")))
    assert len(feats) == 3
    f = feats[0]
    assert f.name == "Happy Go Lucky Occurrence" and f.source_id == "10098555"
    assert (f.lat, f.lon) == (40.5704, -123.37029)  # not swapped
    assert f.attributes["commodities"] == ["CR"] and f.attributes["county_fips"] == "06105"


def test_wfs_fetch_pages_with_start_index(aoi):
    body = fixture_bytes("mrds_page.gml")  # 3 members
    one_member = re.sub(rb"<gml:featureMember>.*?</gml:featureMember>", b"", body, count=2, flags=re.S)
    session = FakeSession([body, one_member])
    pages = list(mrds_wfs.fetch(session, aoi, SINCE, page_size=3))
    assert len(pages) == 2
    assert [c["startIndex"] for c in session.calls] == [0, 3]


def test_wfs_exception_report_raises(aoi):
    report = (b'<ows:ExceptionReport xmlns:ows="http://www.opengis.net/ows"><ows:Exception>'
              b"<ows:ExceptionText>msWFSGetFeature(): bad typename</ows:ExceptionText>"
              b"</ows:Exception></ows:ExceptionReport>")
    with pytest.raises(SourceError, match="bad typename"):
        list(mrds_wfs.fetch(FakeSession([report]), aoi, SINCE))


# --- NOAA GHCN (S3 via boto3) ------------------------------------------------------------------

def test_ghcn_parse_fixed_width_columns():
    raw = page(ghcn_s3, fixture_bytes("ghcnd-stations.txt"), "text/plain")
    feats = {f.source_id: f for f in ghcn_s3.parse(raw)}
    assert len(feats) == 8
    abbott = feats["USC00040006"]
    assert (abbott.lat, abbott.lon) == (39.0333, -122.4333)
    assert abbott.name == "Abbott Mine" and abbott.attributes["state"] == "CA"
    assert abbott.attributes["elevation_m"] == 601.1
    assert feats["AE000041196"].attributes["gsn"] is True
    assert feats["AE000041196"].attributes["wmo_id"] == "41196"


@mock_aws
def test_ghcn_fetch_reads_object_with_aws_sdk(aoi):
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="noaa-ghcn-pds")
    s3.put_object(Bucket="noaa-ghcn-pds", Key="ghcnd-stations.txt", Body=fixture_bytes("ghcnd-stations.txt"))
    pages = list(ghcn_s3.fetch(None, aoi, SINCE, s3_client=s3))
    assert len(pages) == 1 and pages[0].url == "s3://noaa-ghcn-pds/ghcnd-stations.txt"
    assert pages[0].body == fixture_bytes("ghcnd-stations.txt")
