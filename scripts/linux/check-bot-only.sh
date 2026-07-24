#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "$0")/common.sh"

send_test=false
if [[ $# -gt 0 ]]; then
  [[ "$1" == "--send-test-message" && $# -eq 1 ]] || fail 'Usage: check-bot-only.sh [--send-test-message]'
  send_test=true
fi
require_command docker
require_command curl
validate_bot_only_env
cd "$PROJECT_ROOT"
compose config --quiet
compose ps
wait_for_db 10
wait_for_backend 10
compose run --rm migrate >/dev/null

token="$(env_value TELEGRAM_BOT_TOKEN)"
chat_id="$(env_value TELEGRAM_CHAT_ID)"
getme="$(curl -fsS --max-time 15 "https://api.telegram.org/bot${token}/getMe")" || fail 'Telegram getMe request failed'
grep -q '"ok":true' <<<"$getme" || fail 'Telegram getMe returned an error'
getchat="$(curl -fsS --max-time 15 "https://api.telegram.org/bot${token}/getChat?chat_id=${chat_id}")" || fail 'Telegram getChat request failed'
grep -q '"ok":true' <<<"$getchat" || fail 'Telegram chat access check failed'

bot_count="$(compose ps -q bot | grep -c . || true)"
[[ "$bot_count" == "1" ]] || fail "Expected one bot container, found $bot_count"
postgres_user="$(env_value POSTGRES_USER)"
postgres_db="$(env_value POSTGRES_DB)"
heartbeat="$(compose exec -T db psql -U "$postgres_user" -d "$postgres_db" -tAc "SELECT coalesce(worker_id || ' last_poll=' || coalesce(last_poll_at::text, 'never'), 'none') FROM scheduler_heartbeats ORDER BY last_poll_at DESC NULLS LAST LIMIT 1;")"
[[ -n "$heartbeat" && "$heartbeat" != "none" && "$heartbeat" != *'last_poll=never'* ]] || fail 'Scheduler heartbeat is missing or has not completed a poll yet'
printf 'Scheduler heartbeat: %s\n' "$heartbeat"

if [[ "$send_test" == true ]]; then
  response="$(curl -fsS --max-time 15 -X POST "https://api.telegram.org/bot${token}/sendMessage" --data-urlencode "chat_id=${chat_id}" --data-urlencode 'text=PPRBot Linux deployment check')" || fail 'Telegram sendMessage failed'
  grep -q '"ok":true' <<<"$response" || fail 'Telegram test message was rejected'
  printf 'Telegram test message sent.\n'
fi
printf 'Linux bot_only checks passed.\n'
