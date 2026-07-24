#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "$0")/common.sh"

usage() { printf 'Usage: %s <backup.dump> --confirm [--start-bot]\n' "$0" >&2; }
[[ $# -ge 2 ]] || { usage; exit 2; }
backup_path="$1"
shift
confirm=false
start_bot=false
for arg in "$@"; do
  case "$arg" in --confirm) confirm=true ;; --start-bot) start_bot=true ;; *) usage; exit 2 ;; esac
done
[[ "$confirm" == true ]] || fail 'Restore is destructive. Re-run with --confirm.'
[[ -f "$backup_path" ]] || fail "Backup file not found: $backup_path"

require_command docker
require_env_file
cd "$PROJECT_ROOT"
compose stop backend bot >/dev/null 2>&1 || true
compose up -d db
wait_for_db
container="$(db_container_id)"
postgres_user="$(env_value POSTGRES_USER)"
postgres_db="$(env_value POSTGRES_DB)"
container_path="/tmp/pprbot-restore-$(date +%s)-$$.dump"
cleanup() { docker exec "$container" rm -f "$container_path" >/dev/null 2>&1 || true; }
trap cleanup EXIT

printf 'WARNING: current database will be replaced from %s\n' "$(basename "$backup_path")" >&2
printf 'Container pg_restore version: '
docker exec "$container" pg_restore --version
docker cp "$backup_path" "$container:$container_path"
if ! docker exec "$container" pg_restore -l "$container_path" >/dev/null; then
  fail 'Backup is incompatible with the PostgreSQL container pg_restore version or is corrupt.'
fi
docker exec "$container" pg_restore \
  --clean \
  --if-exists \
  --no-owner \
  --no-privileges \
  -U "$postgres_user" \
  -d "$postgres_db" \
  "$container_path"
compose run --rm migrate
compose up -d --no-deps backend
wait_for_backend
if [[ "$start_bot" == true ]]; then
  compose up -d --no-deps bot
fi
printf 'Restore completed. Bot started: %s\n' "$start_bot"
