#!/usr/bin/env bash
# Take a backup, then immediately restore it into a scratch database and check
# the tables came back.
#
#   scripts/backup.sh                    # local docker-compose database
#   DATABASE_URL=postgres://... scripts/backup.sh
#
# The restore is not optional politeness. A backup nobody has restored is a
# file, not a backup -- the failure mode is finding out during the incident.
set -euo pipefail
cd "$(dirname "$0")/.."

DSN="${DATABASE_URL:-postgresql://recur:recur@127.0.0.1:5433/recur}"
DSN="${DSN/postgres:\/\//postgresql://}"
OUT_DIR="${RECUR_BACKUP_DIR:-backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FILE="$OUT_DIR/recur-$STAMP.dump"

mkdir -p "$OUT_DIR"

# A newer pg_dump emits settings an older server cannot parse -- the dump is
# written happily and fails only at restore time, which is the worst possible
# moment to find out. Refuse up front.
CLIENT_MAJOR=$(pg_dump --version | grep -oE '[0-9]+' | head -1)
SERVER_MAJOR=$(psql "$DSN" -qAt -c "SHOW server_version" | grep -oE '^[0-9]+')
if [ "$CLIENT_MAJOR" -gt "$SERVER_MAJOR" ]; then
  cat >&2 <<MSG
pg_dump is version $CLIENT_MAJOR but the server is $SERVER_MAJOR.
A dump taken this way restores with:
    ERROR: unrecognized configuration parameter "transaction_timeout"

Use a matching client. With docker compose running locally:
    docker exec recur-db-1 pg_dump -U recur -d recur -Fc -f /tmp/recur.dump
    docker cp recur-db-1:/tmp/recur.dump ./backups/

On Render, use their managed backups, or run this from a container built on
postgres:$SERVER_MAJOR.
MSG
  exit 1
fi

echo "==> dumping to $FILE"
# -Fc: compressed custom format, so pg_restore can be selective later.
pg_dump --dbname="$DSN" --format=custom --no-owner --file="$FILE"
SIZE=$(du -h "$FILE" | cut -f1)
echo "    $SIZE"

echo "==> verifying by restoring into a scratch database"
SCRATCH="recur_restore_check_$$"
ADMIN="${DSN%/*}/postgres"

createdb --maintenance-db="$ADMIN" "$SCRATCH" 2>/dev/null \
  || psql "$ADMIN" -qc "CREATE DATABASE $SCRATCH" >/dev/null

cleanup() { psql "$ADMIN" -qc "DROP DATABASE IF EXISTS $SCRATCH" >/dev/null 2>&1 || true; }
trap cleanup EXIT

pg_restore --dbname="${DSN%/*}/$SCRATCH" --no-owner --exit-on-error "$FILE" >/dev/null

EXPECTED=$(psql "$DSN" -qAt -c \
  "SELECT count(*) FROM pg_tables WHERE schemaname='public'")
RESTORED=$(psql "${DSN%/*}/$SCRATCH" -qAt -c \
  "SELECT count(*) FROM pg_tables WHERE schemaname='public'")
ROWS=$(psql "${DSN%/*}/$SCRATCH" -qAt -c \
  "SELECT count(*) FROM raw_transaction" 2>/dev/null || echo 0)

echo "    tables: $RESTORED restored / $EXPECTED expected"
echo "    raw_transaction rows in the restore: $ROWS"

if [ "$RESTORED" != "$EXPECTED" ]; then
  echo "FAILED: the restore does not match the source." >&2
  exit 1
fi
echo "==> ok: $FILE restores cleanly"
