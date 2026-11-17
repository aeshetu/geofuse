#!/usr/bin/env bash
# Smoke-test a running GeoFuse API over HTTP. Used by CI against the built container.
#   scripts/smoke.sh [base_url]
set -euo pipefail
BASE="${1:-http://localhost:8000}"
fail() { echo "FAIL: $*" >&2; exit 1; }

check() {  # check <name> <path> <python expression over the parsed JSON body `b`>
  local body
  body="$(curl -fsS "$BASE$2")" || fail "$1: request failed"
  python3 -c "import json,sys; b=json.loads(sys.argv[1]); sys.exit(0 if ($3) else 1)" "$body" \
    || fail "$1: unexpected body: ${body:0:300}"
  echo "ok   $1"
}

check health        "/health"                                              'b == {"postgis": "ok", "elasticsearch": "ok"}'
check features      "/features?bbox=-124.5,37,-119.5,42&kind=mine"         'b["numberReturned"] == 3'
check nearby        "/nearby?lat=40.5704&lon=-123.37029&radius_km=100"     'b["features"][0]["properties"]["distance_km"] == 0'
check search        "/search?q=Hapy%20Go%20Lucky"                          'b["features"][0]["id"] == "mrds_wfs:10098555"'
check latest-run    "/runs/latest"                                         '"quality" in b'
check openapi       "/openapi.json"                                        '"/features" in b["paths"]'

status="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/features?bbox=bad")"
[ "$status" = "422" ] || fail "bad bbox should be 422, got $status"
echo "ok   input-validation"
echo "all smoke checks passed"
