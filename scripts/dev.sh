#!/usr/bin/env bash
# Linux/macOS developer setup: venv, lint, unit tests, then a live extract into a local lake.
#   scripts/dev.sh [--skip-run]
set -euo pipefail
cd "$(dirname "$0")/.."

[ -d .venv ] || python3 -m venv .venv
PY=.venv/bin/python
"$PY" -m pip install --quiet -e ".[dev]"
"$PY" -m ruff check .
"$PY" -m pytest -q

if [ "${1:-}" != "--skip-run" ]; then
  GEOFUSE_STORE=file://data/lake "$PY" -m geofuse.pipeline run --no-load
  latest="$(ls data/lake/runs/*.json | sort | tail -1)"
  echo "Run report: $latest"
  "$PY" -c "import json,sys; print(json.dumps(json.load(open(sys.argv[1]))['quality'], indent=2))" "$latest"
fi
