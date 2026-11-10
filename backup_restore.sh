#!/usr/bin/env bash
# Dark Ship Watch: backup and recovery test.
#   1. pg_dump makes a compressed backup of the live database
#   2. pg_restore --list proves the backup file can be read
#   3. pg_restore restores it into a scratch database called <db>_restore_test
#   4. the row counts of the main tables are compared between the live database and the restored copy
#   5. the scratch database is dropped and only the newest $KEEP backups are kept
#
# Run it with:  bash backup_restore.sh   (Git Bash, WSL, Linux or macOS)
# Settings can be given as environment variables (the defaults fit the portable database):
#   PGHOST=localhost  PGPORT=5433  PGUSER=postgres  PGPASSWORD=...  DSW_DB=darkshipwatch
#   BACKUP_DIR=./backups  KEEP=7  KEEP_SCRATCH=0
#
# If the real database is ever lost:
#   createdb darkshipwatch
#   pg_restore -d darkshipwatch --no-owner backups/<newest>.dump
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PGHOST="${PGHOST:-localhost}"
export PGPORT="${PGPORT:-5433}"
export PGUSER="${PGUSER:-postgres}"
DB="${DSW_DB:-darkshipwatch}"
SCRATCH="${DB}_restore_test"
BACKUP_DIR="${BACKUP_DIR:-$HERE/backups}"
KEEP="${KEEP:-7}"
KEEP_SCRATCH="${KEEP_SCRATCH:-0}"

# locate the PostgreSQL client tools
find_bin() {
  local tool="$1" cand
  if command -v "$tool" >/dev/null 2>&1; then command -v "$tool"; return; fi
  for cand in "${PG_BIN:-}/$tool" "$HERE/.pgportable/pg/bin/$tool" "$HERE/.pgportable/pg/bin/$tool.exe" \
              "/c/Program Files/PostgreSQL/18/bin/$tool.exe" "/c/Program Files/PostgreSQL/17/bin/$tool.exe" \
              "/c/Program Files/PostgreSQL/16/bin/$tool.exe" "/usr/lib/postgresql/18/bin/$tool" "/usr/lib/postgresql/17/bin/$tool" \
              "/usr/lib/postgresql/16/bin/$tool" "/opt/homebrew/bin/$tool"; do
    if [ -x "$cand" ]; then echo "$cand"; return; fi
  done
  echo "ERROR: $tool not found - add the PostgreSQL bin directory to PATH or set PG_BIN" >&2
  exit 2
}
PSQL="$(find_bin psql)"; PG_DUMP="$(find_bin pg_dump)"; PG_RESTORE="$(find_bin pg_restore)"
DROPDB="$(find_bin dropdb)"; CREATEDB="$(find_bin createdb)"

TABLES="vessels location_events latest_position rejected_events vessel_status_history port_events ais_gap_events ports land_polygons weather_obs sea_conditions collector_heartbeat"

count_rows() {                       # count_rows <database> <table>
  "$PSQL" -d "$1" -tAq -c "SELECT count(*) FROM public.$2"
}

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d_%H%M%S)"
DUMP="$BACKUP_DIR/${DB}_${STAMP}.dump"

echo "Step 1 of 5: saving a full copy of the database ($DB)"
declare -A BEFORE AFTER RESTORED
for t in $TABLES; do BEFORE[$t]="$(count_rows "$DB" "$t")"; done
START=$(date +%s)
"$PG_DUMP" -Fc -Z 6 -d "$DB" -f "$DUMP"
for t in $TABLES; do AFTER[$t]="$(count_rows "$DB" "$t")"; done
SIZE=$(wc -c < "$DUMP" | tr -d ' ')
echo "   saved to $DUMP  ($((SIZE / 1024)) KB, $(( $(date +%s) - START )) seconds)"

echo "Step 2 of 5: checking that the copy can be read"
ENTRIES="$("$PG_RESTORE" --list "$DUMP" | grep -vc '^;')"
echo "   the copy is readable and holds $ENTRIES items (tables, indexes, functions and data)"
"$PG_RESTORE" --list "$DUMP" | grep -E ' TABLE DATA public (vessels|location_events|latest_position|ais_gap_events) ' | sed 's/^/   /' || true

echo "Step 3 of 5: restoring the copy into a separate test database"
"$DROPDB" --if-exists "$SCRATCH" >/dev/null 2>&1 || true
"$CREATEDB" "$SCRATCH"
set +e
"$PG_RESTORE" -d "$SCRATCH" --no-owner --exit-on-error "$DUMP" 2> "$BACKUP_DIR/restore_$STAMP.log"
RC=$?
set -e
if [ $RC -ne 0 ]; then
  echo "   pg_restore exited with $RC - first lines of the log:"; head -5 "$BACKUP_DIR/restore_$STAMP.log" | sed 's/^/   /'
else
  echo "   the restore finished without errors"
  rm -f "$BACKUP_DIR/restore_$STAMP.log"
fi

echo "Step 4 of 5: comparing the number of rows in every table"
printf "   %-24s %12s %12s %12s   %s\n" table before_copy after_copy restored result
FAIL=0
for t in $TABLES; do
  RESTORED[$t]="$(count_rows "$SCRATCH" "$t" 2>/dev/null || echo -1)"
  lo=${BEFORE[$t]}; hi=${AFTER[$t]}; if [ "$lo" -gt "$hi" ]; then tmp=$lo; lo=$hi; hi=$tmp; fi
  if [ "${RESTORED[$t]}" -ge "$lo" ] && [ "${RESTORED[$t]}" -le "$hi" ]; then v="same"; else v="DIFFERENT"; FAIL=1; fi
  printf "   %-24s %12s %12s %12s   %s\n" "$t" "${BEFORE[$t]}" "${AFTER[$t]}" "${RESTORED[$t]}" "$v"
done
echo "   (new rows keep arriving while the copy is made, so a restored count between the before and after numbers is correct)"

echo "Step 5 of 5: checking that the map data works in the restored copy"
"$PSQL" -d "$SCRATCH" -tAq -c "SELECT 'PostGIS ' || postgis_lib_version() || ' | ports within 10 km of Helsinki centre: ' || (SELECT count(*) FROM ports WHERE ST_DWithin(geom::geography, ST_SetSRID(ST_MakePoint(24.94,60.17),4326)::geography, 10000)) || ' | land area km2: ' || round((SELECT sum(ST_Area(geom::geography))/1e6 FROM land_polygons)::numeric)" | sed 's/^/   /'

if [ "$KEEP_SCRATCH" != "1" ]; then
  "$DROPDB" --if-exists "$SCRATCH"
  echo "   the test database was removed"
fi

# retention: keep the newest $KEEP dumps
ls -1t "$BACKUP_DIR"/${DB}_*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do rm -f "$old"; echo "   pruned old backup $(basename "$old")"; done

if [ $RC -eq 0 ] && [ $FAIL -eq 0 ]; then
  echo "RESULT: the backup works. The restored copy matches the database."
else
  echo "RESULT: there was a problem. Check the lines above." >&2
  exit 1
fi
