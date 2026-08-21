#!/usr/bin/env bash
# Full local pipeline for one user, from an empty database.
#   scripts/run_all.sh <user-id> [csv] [account]
set -euo pipefail
cd "$(dirname "$0")/.."

USER_ID="${1:?usage: scripts/run_all.sh <user-id> [csv] [account]}"
CSV="${2:-sample.csv}"
ACCOUNT="${3:-demo}"
PY=.venv/bin/python

docker compose up -d >/dev/null
until docker exec recur-db-1 pg_isready -U recur -q; do sleep 1; done
.venv/bin/alembic upgrade head

$PY -m app.core.ingest  "$CSV" --account "$ACCOUNT" --user "$USER_ID"
$PY -m app.core.resolve --user "$USER_ID"
$PY -m app.core.detect  --user "$USER_ID"
