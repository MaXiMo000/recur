#!/usr/bin/env bash
# Full pipeline from an empty database. Also the smoke test.
set -euo pipefail
PY=.venv/bin/python
CSV="${1:-sample.csv}"
ACCOUNT="${2:-demo}"

docker compose up -d >/dev/null
until docker exec recur-db-1 pg_isready -U recur -q; do sleep 1; done

$PY ingest.py "$CSV" --account "$ACCOUNT"
$PY resolve.py
$PY detect.py
