#!/usr/bin/env bash
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
"$SCRIPT_DIR/stop-bot-only.sh"
"$SCRIPT_DIR/start-bot-only.sh"
