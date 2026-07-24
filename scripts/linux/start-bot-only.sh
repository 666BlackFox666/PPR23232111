#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "$0")/common.sh"

require_command docker
require_command curl
validate_bot_only_env
ensure_runtime_dirs
cd "$PROJECT_ROOT"

compose config --quiet
compose build
compose up -d db
wait_for_db
compose run --rm migrate
compose up -d --no-deps backend
wait_for_backend
compose up -d --no-deps bot

printf 'PPRBot Linux bot_only stack started.\n'
printf 'Backend: http://127.0.0.1:8000/health\n'
print_safe_flags
