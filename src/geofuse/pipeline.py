"""ETL orchestration and CLI.

    geofuse run                       extract all sources live -> lake -> PostGIS + Elasticsearch
    geofuse run --no-load             extract + validate into the lake only (no databases needed)
    geofuse run --replay <run_id>     re-parse a past run's raw pages from the lake, no upstream calls
    geofuse init-db                   create the PostGIS schema and Elasticsearch index
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from geofuse.config import Settings
from geofuse.http import make_session
from geofuse.model import Feature
from geofuse.sources import RawPage, registry
from geofuse.storage import ObjectStore, get_json, open_store, put_json, put_jsonl_gz
from geofuse.transform import transform

log = logging.getLogger("geofuse")


def new_run_id(now: datetime) -> str:
    return now.strftime("%Y%m%dT%H%M%SZ")


def raw_prefix(source: str, run_id: str) -> str:
    day = datetime.strptime(run_id, "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%d")
    return f"raw/{source}/dt={day}/run={run_id}"


def land_raw(store: ObjectStore, pages: list[RawPage], run_id: str) -> dict:
    """Write raw pages exactly as received, plus a manifest with checksums for replay and audit."""
    import gzip

    entries = []
    for p in pages:
        key = f"{raw_prefix(p.source, run_id)}/page-{p.seq:04d}.{p.extension}.gz"
        store.put(key, gzip.compress(p.body), "application/gzip")
        entries.append({"seq": p.seq, "key": key, "url": p.url, "content_type": p.content_type,
                        "bytes": len(p.body), "sha256": hashlib.sha256(p.body).hexdigest()})
    if pages:
        put_json(store, f"{raw_prefix(pages[0].source, run_id)}/manifest.json", {"pages": entries})
    return {"pages": len(entries), "bytes": sum(e["bytes"] for e in entries)}


def read_raw(store: ObjectStore, source: str, run_id: str) -> list[RawPage]:
    import gzip

    manifests = [k for k in store.list(f"raw/{source}/") if f"/run={run_id}/" in k and k.endswith("manifest.json")]
    if not manifests:
        return []
    pages = []
    for e in get_json(store, manifests[0])["pages"]:
        body = gzip.decompress(store.get(e["key"]))
        if hashlib.sha256(body).hexdigest() != e["sha256"]:
            raise RuntimeError(f"checksum mismatch for {e['key']}")
        pages.append(RawPage(source, e["seq"], e["url"], e["content_type"], body))
    return pages


def run(settings: Settings, sources: list[str], since: datetime, store: ObjectStore,
        load: bool = True, replay: str | None = None, session=None, s3_client=None,
        pg_conn=None, es=None) -> dict:
    started = datetime.now(timezone.utc)
    run_id = new_run_id(started)
    day = started.strftime("%Y-%m-%d")
    mods = registry()
    report: dict = {"run_id": run_id, "replay_of": replay, "aoi": str(settings.aoi),
                    "since": since.isoformat(), "started_at": started.isoformat(), "sources": {}}
    session = session or make_session()
    features: list[Feature] = []
    per_source: Counter = Counter()

    for name in sources:
        mod, t0 = mods[name], time.perf_counter()
        if replay:
            pages = read_raw(store, name, replay)
        else:
            pages = list(mod.fetch(session, settings.aoi, since, s3_client=s3_client))
        landed = land_raw(store, pages, run_id) if not replay else {"pages": len(pages), "bytes": None}
        parsed = [f for p in pages for f in mod.parse(p)]
        features += parsed
        per_source[name] = len(parsed)
        report["sources"][name] = {**landed, "parsed": len(parsed),
                                   "seconds": round(time.perf_counter() - t0, 2)}
        log.info("%-12s %5d pages  %7d records  %.1fs", name, landed["pages"], len(parsed),
                 time.perf_counter() - t0)

    result = transform(features, settings.aoi, started)
    by_source_valid = Counter(f.source for f in result.valid)
    for name in sources:
        report["sources"][name]["valid"] = by_source_valid[name]
    put_jsonl_gz(store, f"curated/dt={day}/run={run_id}/features.jsonl.gz", (f.to_record() for f in result.valid))
    if result.rejects:
        put_jsonl_gz(store, f"quarantine/dt={day}/run={run_id}/rejects.jsonl.gz", result.rejects)
    report["quality"] = {
        "parsed": len(features), "valid": len(result.valid), "rejected": len(result.rejects),
        "outside_aoi": result.outside_aoi, "duplicates": result.duplicates,
        "reject_reasons": dict(result.reasons),
    }

    if load:
        from geofuse import load as loader

        report["loaded"] = {
            "postgis": loader.load_postgis(pg_conn, result.valid, run_id),
            "elasticsearch": loader.load_elasticsearch(es, settings.es_index, result.valid, run_id),
        }

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    put_json(store, f"runs/{run_id}.json", report)
    if load:
        loader.record_run(pg_conn, report)
    return report


def connect(settings: Settings):
    import psycopg
    from elasticsearch import Elasticsearch

    return psycopg.connect(settings.pg_dsn), Elasticsearch(settings.es_url)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="geofuse", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the ETL")
    r.add_argument("--sources", nargs="+", choices=sorted(registry()), default=sorted(registry()))
    r.add_argument("--since-days", type=int, default=30, help="look-back window for event sources")
    r.add_argument("--no-load", action="store_true", help="stop after landing curated data in the lake")
    r.add_argument("--replay", metavar="RUN_ID", help="re-parse a previous run's raw pages")
    sub.add_parser("init-db", help="create the PostGIS schema and Elasticsearch index")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if args.cmd == "init-db":
        from geofuse import load as loader

        pg, es = connect(settings)
        loader.init_db(pg)
        loader.ensure_index(es, settings.es_index)
        log.info("schema and index ready")
        return 0

    store = open_store(settings.store_url, settings.s3_endpoint)
    pg = es = None
    if not args.no_load:
        pg, es = connect(settings)
    since = datetime.now(timezone.utc) - timedelta(days=args.since_days)
    report = run(settings, args.sources, since, store, load=not args.no_load, replay=args.replay,
                 pg_conn=pg, es=es)
    q = report["quality"]
    log.info("run %s: %d valid, %d rejected, %d outside AOI, %d duplicates", report["run_id"],
             q["valid"], q["rejected"], q["outside_aoi"], q["duplicates"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
