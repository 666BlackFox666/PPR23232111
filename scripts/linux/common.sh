#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE=(docker compose -f "$PROJECT_ROOT/docker-compose.yml" -f "$PROJECT_ROOT/docker-compose.linux.yml")

compose() {
  "${COMPOSE[@]}" "$@"
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1"
}

require_env_file() {
  [[ -f "$PROJECT_ROOT/.env" ]] || fail "Missing $PROJECT_ROOT/.env. Copy .env.linux.example and configure it."
}

env_value() {
  local key="$1"
  local line
  line="$(grep -E "^${key}=" "$PROJECT_ROOT/.env" | tail -n 1 || true)"
  printf '%s' "${line#*=}" | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"
}

require_nonempty_env() {
  local key="$1"
  [[ -n "$(env_value "$key")" ]] || fail "$key must be set in .env"
}

validate_bot_only_env() {
  local auto_send mass_limit pilot_allowed
  require_env_file
  [[ "$(env_value DEPLOYMENT_MODE)" == "bot_only" ]] || fail "DEPLOYMENT_MODE must be bot_only"
  [[ "$(env_value TELEGRAM_ENABLED)" == "true" ]] || fail "TELEGRAM_ENABLED must be true"
  [[ "$(env_value DEV_COMMANDS_ENABLED)" == "false" ]] || fail "DEV_COMMANDS_ENABLED must be false"
  [[ "$(env_value AUTO_SEND_ALLOW_MASS)" == "false" ]] || fail "AUTO_SEND_ALLOW_MASS must be false"

  mass_limit="$(env_value AUTO_SEND_MASS_LIMIT)"
  [[ "$mass_limit" =~ ^([1-9]|10)$ ]] || fail "AUTO_SEND_MASS_LIMIT must be an integer from 1 to 10"

  auto_send="$(env_value NOTIFICATIONS_AUTO_SEND_ENABLED)"
  pilot_allowed="$(env_value PILOT_AUTO_SEND_ALLOWED)"
  if [[ "$auto_send" == "true" && "$pilot_allowed" != "true" ]]; then
    fail "PILOT_AUTO_SEND_ALLOWED must be true when NOTIFICATIONS_AUTO_SEND_ENABLED=true"
  fi

  require_nonempty_env TELEGRAM_BOT_TOKEN
  require_nonempty_env TELEGRAM_CHAT_ID
  require_nonempty_env ADMIN_TELEGRAM_IDS
  require_nonempty_env POSTGRES_DB
  require_nonempty_env POSTGRES_USER
  require_nonempty_env POSTGRES_PASSWORD
  require_nonempty_env DATABASE_URL
}

ensure_runtime_dirs() {
  mkdir -p "$PROJECT_ROOT/logs" "$PROJECT_ROOT/backups"
  chmod 700 "$PROJECT_ROOT/backups"
}

db_container_id() {
  compose ps -q db
}

wait_for_db() {
  local timeout="${1:-90}" container status elapsed=0
  container="$(db_container_id)"
  [[ -n "$container" ]] || fail "PostgreSQL container was not created"
  while (( elapsed < timeout )); do
    status="$(docker inspect -f '{{.State.Health.Status}}' "$container" 2>/dev/null || true)"
    [[ "$status" == "healthy" ]] && return 0
    sleep 3
    elapsed=$((elapsed + 3))
  done
  fail "PostgreSQL did not become healthy within ${timeout}s"
}

wait_for_backend() {
  local timeout="${1:-90}" elapsed=0
  while (( elapsed < timeout )); do
    if curl -fsS --max-time 3 http://127.0.0.1:8000/health >/dev/null 2>&1; then
      return 0
    fi
    sleep 3
    elapsed=$((elapsed + 3))
  done
  fail "Backend /health did not become available within ${timeout}s"
}

print_safe_flags() {
  local key
  for key in DEPLOYMENT_MODE TELEGRAM_ENABLED NOTIFICATIONS_AUTO_SEND_ENABLED PILOT_AUTO_SEND_ALLOWED AUTO_SEND_ALLOW_MASS AUTO_SEND_MASS_LIMIT DEV_COMMANDS_ENABLED; do
    printf '%s=%s\n' "$key" "$(env_value "$key")"
  done
}
