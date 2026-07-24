#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "$0")/common.sh"

require_command docker
require_env_file
cd "$PROJECT_ROOT"

printf 'PPRBot Linux bot_only status\n'
compose ps
container="$(db_container_id)"
printf 'PostgreSQL health: %s\n' "$(docker inspect -f '{{.State.Health.Status}}' "$container" 2>/dev/null || printf 'not running')"
if curl -fsS --max-time 3 http://127.0.0.1:8000/health >/dev/null 2>&1; then
  printf 'Backend health: healthy\n'
else
  printf 'Backend health: unavailable\n'
fi
bot_count="$(compose ps -q bot | grep -c . || true)"
printf 'Bot containers: %s\n' "$bot_count"
printf 'Frontend service present: %s\n' "$(compose config --services | grep -qx frontend && printf yes || printf no)"
printf 'Cloudflared service present: %s\n' "$(compose config --services | grep -qx cloudflared && printf yes || printf no)"
printf 'Safe .env flags\n'
print_safe_flags
