#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "$0")/common.sh"

[[ "${EUID}" -eq 0 ]] || fail 'Run install-systemd.sh with sudo.'
[[ "$PROJECT_ROOT" == "/opt/pprbot" ]] || fail "Expected project root /opt/pprbot, got $PROJECT_ROOT"
[[ -f "$PROJECT_ROOT/.env" ]] || fail 'Missing /opt/pprbot/.env'

install -m 0644 "$PROJECT_ROOT/deploy/systemd/pprbot-bot-only.service" /etc/systemd/system/pprbot-bot-only.service
install -m 0644 "$PROJECT_ROOT/deploy/systemd/pprbot-backup.service" /etc/systemd/system/pprbot-backup.service
install -m 0644 "$PROJECT_ROOT/deploy/systemd/pprbot-backup.timer" /etc/systemd/system/pprbot-backup.timer
systemctl daemon-reload
systemctl enable pprbot-bot-only.service
systemctl enable --now pprbot-backup.timer
printf 'Systemd units installed. pprbot-bot-only.service is enabled but not started.\n'
