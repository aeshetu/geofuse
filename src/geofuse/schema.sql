-- GeoFuse PostGIS schema. Idempotent: safe to run on every deploy.
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS features (
    uid             text PRIMARY KEY,               -- "<source>:<source_id>", stable across runs
    source          text        NOT NULL,
    source_id       text        NOT NULL,
    kind            text        NOT NULL CHECK (kind IN ('earthquake', 'wildfire', 'mine', 'weather_station')),
    name            text        NOT NULL,
    observed_at     timestamptz,                    -- NULL for static sites
    attributes      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    geom            geography(Point, 4326) NOT NULL,
    first_seen_run  text        NOT NULL,
    last_seen_run   text        NOT NULL,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Spatial index for bbox (&&) and radius (ST_DWithin) queries, plus KNN ordering (<->).
CREATE INDEX IF NOT EXISTS features_geom_gix ON features USING gist (geom);
-- "Latest earthquakes / fires" is the most common filter.
CREATE INDEX IF NOT EXISTS features_kind_time_idx ON features (kind, observed_at DESC);

CREATE TABLE IF NOT EXISTS etl_runs (
    run_id      text PRIMARY KEY,
    started_at  timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    report      jsonb       NOT NULL
);
