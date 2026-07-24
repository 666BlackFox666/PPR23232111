#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "$0")/common.sh"

require_command docker
require_env_file
ensure_runtime_dirs
cd "$PROJECT_ROOT"
compose up -d db
wait_for_db

stamp="$(date +%Y%m%d-%H%M%S)"
filename="pprbot-${stamp}.dump"
destination="$PROJECT_ROOT/backups/$filename"
container_path="/tmp/$filename"
container="$(db_container_id)"
postgres_user="$(env_value POSTGRES_USER)"
postgres_db="$(env_value POSTGRES_DB)"

cleanup() { docker exec "$container" rm -f "$container_path" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker exec "$container" pg_dump -U "$postgres_user" -d "$postgres_db" -Fc -f "$container_path"
docker cp "$container:$container_path" "$destination"
[[ -s "$destination" ]] || fail "Backup file was not created"
chmod 600 "$destination"

mapfile -t old_backups < <(find "$PROJECT_ROOT/backups" -maxdepth 1 -type f -name 'pprbot-*.dump' -printf '%T@ %p\n' | sort -rn | awk 'NR>14 {print $2}')
for backup in "${old_backups[@]:-}"; do
  [[ -n "$backup" ]] && rm -f -- "$backup"
done
printf 'Backup created: %s\n' "$destination"
