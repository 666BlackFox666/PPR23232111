#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "$0")/common.sh"

require_command docker
require_env_file
cd "$PROJECT_ROOT"
compose down --remove-orphans
printf 'PPRBot Linux bot_only stack stopped. Database volume was preserved.\n'
