"""Loaders: PostGIS (system of record) and Elasticsearch (search index). Both are idempotent upserts."""

from __future__ import annotations

import json
from collections.abc import Sequence
from importlib.resources import files

from geofuse.model import Feature

SCHEMA_SQL = files("geofuse").joinpath("schema.sql").read_text()
ES_MAPPING = json.loads(files("geofuse").joinpath("es_mapping.json").read_text())

UPSERT_SQL = """
INSERT INTO features (uid, source, source_id, kind, name, observed_at, attributes, geom,
                      first_seen_run, last_seen_run)
SELECT uid, source, source_id, kind, name, observed_at, attributes,
       ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography, %(run)s, %(run)s
FROM stage
ON CONFLICT (uid) DO UPDATE SET
    name = EXCLUDED.name, observed_at = EXCLUDED.observed_at, attributes = EXCLUDED.attributes,
    geom = EXCLUDED.geom, last_seen_run = EXCLUDED.last_seen_run, updated_at = now()
"""


def init_db(conn) -> None:
    conn.execute(SCHEMA_SQL)
    conn.commit()


def load_postgis(conn, features: Sequence[Feature], run_id: str) -> int:
    """COPY into a temp staging table, then one set-based upsert; all-or-nothing per run."""
    with conn.cursor() as cur:
        cur.execute("""CREATE TEMP TABLE stage (uid text, source text, source_id text, kind text, name text,
                       observed_at timestamptz, attributes jsonb, lon float8, lat float8) ON COMMIT DROP""")
        with cur.copy("COPY stage FROM STDIN") as copy:
            for f in features:
                copy.write_row((f.uid, f.source, f.source_id, f.kind, f.name, f.observed_at,
                                json.dumps(f.attributes, default=str), f.lon, f.lat))
        cur.execute(UPSERT_SQL, {"run": run_id})
        n = cur.rowcount
    conn.commit()
    return n


def record_run(conn, report: dict) -> None:
    conn.execute(
        """INSERT INTO etl_runs (run_id, started_at, finished_at, report) VALUES (%s, %s, %s, %s)
           ON CONFLICT (run_id) DO UPDATE SET finished_at = EXCLUDED.finished_at, report = EXCLUDED.report""",
        (report["run_id"], report["started_at"], report["finished_at"], json.dumps(report, default=str)),
    )
    conn.commit()


def es_document(f: Feature, run_id: str) -> dict:
    return {
        "uid": f.uid, "source": f.source, "source_id": f.source_id, "kind": f.kind, "name": f.name,
        "observed_at": f.observed_at.isoformat() if f.observed_at else None,
        "location": {"lat": f.lat, "lon": f.lon},
        # flattened fields hold strings; drop nulls so they don't index as "None"
        "attributes": {k: v if isinstance(v, (str, list)) else json.dumps(v)
                       for k, v in f.attributes.items() if v is not None},
        "run_id": run_id,
    }


def ensure_index(es, index: str) -> None:
    if not es.indices.exists(index=index):
        es.indices.create(index=index, settings=ES_MAPPING["settings"], mappings=ES_MAPPING["mappings"])


def load_elasticsearch(es, index: str, features: Sequence[Feature], run_id: str) -> int:
    from elasticsearch import helpers

    ensure_index(es, index)
    actions = ({"_index": index, "_id": f.uid, "_source": es_document(f, run_id)} for f in features)
    ok, errors = helpers.bulk(es, actions, chunk_size=2000, raise_on_error=False, refresh="wait_for")
    if errors:
        raise RuntimeError(f"Elasticsearch rejected {len(errors)} documents, first: {errors[0]}")
    return ok
