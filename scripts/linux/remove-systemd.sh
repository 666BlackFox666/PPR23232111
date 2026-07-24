#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "$0")/common.sh"

[[ "${EUID}" -eq 0 ]] || fail 'Run remove-systemd.sh with sudo.'
systemctl disable --now pprbot-backup.timer >/dev/null 2>&1 || true
systemctl disable --now pprbot-bot-only.service >/dev/null 2>&1 || true
rm -f /etc/systemd/system/pprbot-bot-only.service /etc/systemd/system/pprbot-backup.service /etc/systemd/system/pprbot-backup.timer
systemctl daemon-reload
printf 'PPRBot systemd units removed. Docker volumes were not removed.\n'
