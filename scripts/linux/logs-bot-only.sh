#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "$0")/common.sh"

usage() { printf 'Usage: %s {all|backend|bot|db} [--tail N] [--follow]\n' "$0" >&2; }
[[ $# -ge 1 ]] || { usage; exit 2; }
service="$1"
shift
case "$service" in all) services=(db backend bot) ;; backend|bot|db) services=("$service") ;; *) usage; exit 2 ;; esac
cd "$PROJECT_ROOT"
compose logs "$@" "${services[@]}"
