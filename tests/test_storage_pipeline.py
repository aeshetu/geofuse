import boto3
import pytest
from conftest import CONUS, FakeSession, fixture_bytes
from moto import mock_aws

from geofuse import pipeline
from geofuse.config import Settings
from geofuse.storage import LocalStore, S3Store, get_json, get_jsonl_gz, open_store, put_jsonl_gz

ALL = ["usgs_quakes", "wfigs_fires", "mrds_wfs", "ghcn_s3"]


def settings(aoi) -> Settings:
    return Settings(aoi=aoi, store_url="", s3_endpoint=None, pg_dsn="", es_url="", es_index="test")


def fake_sources():
    """Upstream responses in the order the pipeline requests them (sources run in list order)."""
    single_page = fixture_bytes("wfigs_page.json").replace(b'"exceededTransferLimit":true', b'"x":1')
    session = FakeSession([fixture_bytes("quakes.geojson"), single_page, fixture_bytes("mrds_page.gml")])
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="noaa-ghcn-pds")
    s3.put_object(Bucket="noaa-ghcn-pds", Key="ghcnd-stations.txt", Body=fixture_bytes("ghcnd-stations.txt"))
    return session, s3


def test_local_store_round_trip(tmp_path):
    store = LocalStore(tmp_path)
    put_jsonl_gz(store, "a/b/x.jsonl.gz", [{"n": 1}, {"n": 2}])
    assert store.list("a/") == ["a/b/x.jsonl.gz"]
    assert get_jsonl_gz(store, "a/b/x.jsonl.gz") == [{"n": 1}, {"n": 2}]


@mock_aws
def test_s3_store_round_trip_with_prefix():
    store = S3Store("lake", prefix="geofuse", client=boto3.client("s3", region_name="us-east-1"), create=True)
    store.put("raw/x.txt", b"hello")
    assert store.list("raw/") == ["raw/x.txt"]
    assert store.get("raw/x.txt") == b"hello"


def test_open_store_rejects_unknown_scheme():
    with pytest.raises(ValueError):
        open_store("ftp://nope")


@mock_aws
def test_pipeline_lands_raw_curated_and_report_then_replays(tmp_path):
    store = LocalStore(tmp_path)
    session, s3 = fake_sources()
    report = pipeline.run(settings(CONUS), ALL, pipeline.datetime(2026, 11, 1, tzinfo=pipeline.timezone.utc),
                          store, load=False, session=session, s3_client=s3)

    q = report["quality"]
    assert q["parsed"] == 3 + 3 + 3 + 8
    # 3 non-US GHCN stations fall outside CONUS; everything else is valid.
    assert q["outside_aoi"] == 3 and q["valid"] == 14 and q["rejected"] == 0
    assert {s: v["pages"] for s, v in report["sources"].items()} == dict.fromkeys(ALL, 1)

    keys = store.list("")
    assert any(k.startswith("raw/mrds_wfs/") and k.endswith("page-0001.xml.gz") for k in keys)
    assert sum(k.endswith("manifest.json") for k in keys) == 4
    curated = [k for k in keys if k.startswith("curated/")]
    assert len(get_jsonl_gz(store, curated[0])) == 14
    assert get_json(store, f"runs/{report['run_id']}.json")["quality"] == q

    # Replay re-parses the landed raw pages with no network access at all.
    replay = pipeline.run(settings(CONUS), ALL, pipeline.datetime(2026, 11, 1, tzinfo=pipeline.timezone.utc),
                          store, load=False, replay=report["run_id"], session=FakeSession([]))
    assert replay["quality"] == q and replay["replay_of"] == report["run_id"]


def test_replay_detects_tampered_raw_page(tmp_path):
    import gzip

    store = LocalStore(tmp_path)
    page = pipeline.RawPage("usgs_quakes", 1, "u", "application/json", fixture_bytes("quakes.geojson"))
    pipeline.land_raw(store, [page], "20261116T000000Z")
    key = next(k for k in store.list("raw/") if k.endswith(".json.gz"))
    store.put(key, gzip.compress(b"{}"))
    with pytest.raises(RuntimeError, match="checksum"):
        pipeline.read_raw(store, "usgs_quakes", "20261116T000000Z")
