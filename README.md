# PPRBot

Проект для уведомлений по ППР:

- Excel — источник расписания.
- Backend FastAPI — импорт, API, БД, планировщик.
- Telegram Bot — уведомления в чат и inline-кнопки.
- Telegram Mini App — карточки ППР, статусы, история.
- Outlook Graph — автоматический поиск события календаря и добавление ссылки в уведомление.

## Структура

```text
backend/   FastAPI + aiogram + SQLAlchemy
frontend/  React + TypeScript + Vite
data/      schedule.xlsx
alembic/   миграции БД
```

## Режимы развертывания

Проект использует одну бизнес-логику и переключается только настройкой `.env`:

- `DEPLOYMENT_MODE=bot_only` — безопасный первый пилот: PostgreSQL, FastAPI и Telegram Bot. Mini App, Vite и Cloudflare не запускаются, публичный HTTPS URL не требуется.
- `DEPLOYMENT_MODE=full` — полный режим с Telegram Mini App, frontend и Cloudflare Tunnel.

Безопасный дефолт в `.env.example`:

```env
DEPLOYMENT_MODE=bot_only
DEV_COMMANDS_ENABLED=false
TELEGRAM_ENABLED=true
NOTIFICATIONS_AUTO_SEND_ENABLED=false
PILOT_AUTO_SEND_ALLOWED=false
AUTO_SEND_MASS_LIMIT=10
AUTO_SEND_ALLOW_MASS=false
WEBAPP_URL=
TELEGRAM_MINIAPP_SHORT_NAME=
```

PostgreSQL публикуется Docker Compose только на `127.0.0.1:5432`. Backend и Vite также слушают только `127.0.0.1`; наружу они не открываются.

## Первый пилот: bot_only на Windows

Укажите `DEPLOYMENT_MODE=bot_only` в `.env`, затем запустите двойным кликом:

```text
start-pprbot-bot-only.bat
```

Или вручную:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\start-bot-only.ps1"
```

Скрипт проверяет Docker, `.venv\Scripts\python.exe` и `.env`, запускает PostgreSQL, ждет healthcheck, применяет Alembic, запускает backend без `--reload` на `127.0.0.1:8000` и bot runner. PID сохраняются в `.runtime\bot-only\`.

Не запускаются `npm`, frontend, `cloudflared` и Mini App. `WEBAPP_URL`, `TELEGRAM_MINIAPP_SHORT_NAME` и настройка Main App в BotFather для этого режима не нужны.

Остановка:

```text
stop-pprbot-bot-only.bat
```

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\stop-bot-only.ps1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\stop-bot-only.ps1" -StopDatabase
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\status-bot-only.ps1"
```

В bot_only карточка не открывается как WebApp. Telegram-сообщение содержит `Взять в работу`/`Проверено` по статусу и `Подробнее`; последняя кнопка выводит полную карточку ППР в Telegram, включая Outlook-ссылку при наличии.

## Bot-only deployment на Windows

На отдельной машине нужны Docker Desktop с Docker Compose, Python virtualenv проекта, `psycopg`/Alembic из `backend\requirements.txt` и доступ в интернет к `api.telegram.org:443`. Node.js, cloudflared и BotFather Main App для bot_only не требуются.

1. Скопируйте проект в `C:\Projects\PPRBot`.
2. Создайте `.env` из `.env.example` и заполните только production-параметры, не публикуя файл:

```env
DEPLOYMENT_MODE=bot_only
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=-100...
DATABASE_URL=postgresql+psycopg://ppr_user:ppr_password@localhost:5432/ppr_db
ADMIN_TELEGRAM_IDS=123456789
DEV_COMMANDS_ENABLED=false
NOTIFICATIONS_AUTO_SEND_ENABLED=false
PILOT_AUTO_SEND_ALLOWED=false
AUTO_SEND_ALLOW_MASS=false
AUTO_SEND_MASS_LIMIT=10
WEBAPP_URL=
TELEGRAM_MINIAPP_SHORT_NAME=
```

`start-bot-only.ps1` проверяет эти значения до запуска Docker и bot runner. Токены, пароль БД, `initData` и Outlook secret не выводятся в консоль скриптов.

Первый запуск и безопасная проверка:

```powershell
.\scripts\start-bot-only.ps1
.\scripts\check-bot-only.ps1
.\scripts\pilot-smoke-test.ps1
```

Параметры `-SendTestMessage` для `check-bot-only.ps1` и `-IncludeTelegramMessage` для `pilot-smoke-test.ps1` отправляют ровно одно сообщение `PPRBot: проверка подключения`; без них внешние сообщения не отправляются.

Логи: `logs\backend.log`, `logs\bot.log`, `logs\startup.log`, `logs\errors.log`. При старте они ротируются при 5 MB, хранится основной файл и до 9 архивных копий каждого лога.

Автозапуск выполняется от `SYSTEM` с повышенными правами, запускается при старте Windows и повторяет попытку до трех раз с интервалом пять минут, если Docker еще не готов:

```powershell
# Запускать в elevated PowerShell
.\scripts\install-bot-only-autostart.ps1
.\scripts\install-db-backup-task.ps1

# Удаление задач
.\scripts\remove-bot-only-autostart.ps1
.\scripts\remove-db-backup-task.ps1
```

Задачи называются `PPRBot Bot Only` и `PPRBot Database Backup`; их статус показывает `scripts\status-bot-only.ps1`.

Резервное копирование PostgreSQL выполняется в custom-формате `pg_dump`, файлы сохраняются в `backups\`, а старше 14 копий удаляются:

```powershell
.\scripts\backup-db.ps1
.\scripts\restore-db.ps1 -BackupPath .\backups\pprbot-YYYYMMDD-HHmmss.dump
.\scripts\restore-db.ps1 -BackupPath .\backups\pprbot-YYYYMMDD-HHmmss.dump -Confirm
```

### Полная замена расписания из Excel

Для полной замены расписания используется отдельная CLI-команда. Она удаляет только `ppr_events`, `ppr_notifications` и старые `import_runs`; пользователи, роли, `audit_log` и история Alembic сохраняются. Перед применением команда требует выключенную автоотправку, отсутствие `processing`-уведомлений, создаёт PostgreSQL backup в `backups\` и использует PostgreSQL advisory lock.

Сначала обязательно выполните preview:

```powershell
$env:PYTHONPATH="backend"
.\.venv\Scripts\python.exe -m app.cli.replace_schedule_from_excel --file data/schedule.xlsx --preview

# Только диагностический XLSX-отчёт; БД и исходный Excel не изменяются
.\.venv\Scripts\python.exe -m app.cli.replace_schedule_from_excel --file data/schedule.xlsx --export-validation-report data/schedule_validation.xlsx
```

Команда не применяет изменения без точного подтверждения:

```powershell
.\.venv\Scripts\python.exe -m app.cli.replace_schedule_from_excel --file data/schedule.xlsx --apply --confirm REPLACE_SCHEDULE
```

Для применения `NOTIFICATIONS_AUTO_SEND_ENABLED` должен быть `false`, а bot runner следует остановить. Недатированная непустая строка Excel допустима: она создаёт карточку в разделе «Без даты» без notification. Для неё используется `replace-row:<лист>:<номер строки>` только в режиме полной замены. Дата в прошлом, дата без времени, повтор явно заданного Excel ID или дублирующийся `source_key` блокируют apply. Совпадающий fingerprint отображается предупреждением и создаёт отдельные карточки. После применения ID PostgreSQL не переиспользуются, поэтому старые Telegram-ссылки не укажут на новые ППР.

Восстановление без `-Confirm` запрещено. С `-Confirm` оно заменяет данные `ppr_db`; автоматически restore никогда не запускается. Задача `PPRBot Database Backup` запускает backup ежедневно в 03:00.

При обновлении проекта: остановите bot-only, обновите файлы и зависимости, выполните `alembic upgrade head`, затем запустите `start-bot-only.ps1` и `pilot-smoke-test.ps1`. После плановой перезагрузки Windows Task Scheduler запускает тот же стартовый скрипт; проверьте его результат через `status-bot-only.ps1` и логи.

### Checklist перед пилотом

- `DEV_COMMANDS_ENABLED=false`.
- `NOTIFICATIONS_AUTO_SEND_ENABLED=false`.
- `AUTO_SEND_ALLOW_MASS=false`.
- `ADMIN_TELEGRAM_IDS` содержит реального администратора.

### Linux bot_only deployment — AlmaLinux 9

Linux-развёртывание использует `docker-compose.yml` вместе с `docker-compose.linux.yml`. Стек состоит только из `db`, одноразового `migrate`, `backend` и одного `bot`; frontend и cloudflared в нём отсутствуют. PostgreSQL остаётся привязанным к `127.0.0.1:5432`, backend — к `127.0.0.1:8000`.

На AlmaLinux 9 установите Docker Engine с Compose plugin. Выполняйте установку от пользователя с `sudo`; после добавления в группу `docker` перелогиньтесь:

```bash
sudo dnf -y install dnf-plugins-core
sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
sudo dnf -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
# Перелогиньтесь, затем убедитесь, что Docker доступен без sudo.
docker --version
docker compose version
```

На AlmaLinux подготовьте каталог и проект:

```bash
sudo mkdir -p /opt/pprbot
sudo chown "$USER":"$USER" /opt/pprbot
git clone <REPOSITORY_URL> /opt/pprbot
cd /opt/pprbot
cp .env.linux.example .env
chmod 600 .env
chmod +x scripts/linux/*.sh
```

Заполните `.env`, в том числе URL-encoded пароль в `DATABASE_URL`, затем проверьте конфигурацию и соберите image:

```bash
docker compose -f docker-compose.yml -f docker-compose.linux.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.linux.yml build
```

Первый запуск backend без бота полезен при переносе данных:

```bash
docker compose -f docker-compose.yml -f docker-compose.linux.yml up -d db
docker compose -f docker-compose.yml -f docker-compose.linux.yml run --rm migrate
docker compose -f docker-compose.yml -f docker-compose.linux.yml up -d --no-deps backend
curl -fsS http://127.0.0.1:8000/health
```

Перед переключением остановите Windows bot runner. Затем на Linux восстановите Windows custom-format backup (он совместим с PostgreSQL 17 container):

```bash
./scripts/linux/restore-db.sh ./backups/pprbot-20260723-164618.dump --confirm
./scripts/linux/start-bot-only.sh
./scripts/linux/check-bot-only.sh
```

Обычные операции:

```bash
./scripts/linux/status-bot-only.sh
./scripts/linux/logs-bot-only.sh bot --tail 200 --follow
./scripts/linux/backup-db.sh
./scripts/linux/restart-bot-only.sh
```

`check-bot-only.sh` не отправляет сообщений. Явная проверка отправки доступна только с `--send-test-message`.

Systemd устанавливается отдельно и не запускает restore:

```bash
sudo ./scripts/linux/install-systemd.sh
sudo systemctl start pprbot-bot-only.service
systemctl status pprbot-bot-only.service
```

При обновлении: остановите stack, обновите проект, выполните `./scripts/linux/start-bot-only.sh`, затем `./scripts/linux/check-bot-only.sh`. Для rollback остановите stack и выполните `restore-db.sh <dump> --confirm`; бот после restore не запускается без `--start-bot`.

Обычное удаление stack сохраняет volume БД:

```bash
./scripts/linux/stop-bot-only.sh
```

Опасное полное удаление данных выполняется только вручную и удаляет volume:

```bash
docker compose -f docker-compose.yml -f docker-compose.linux.yml down -v
```
- Выполнены `/sendtest <id>`, `Взять в работу` и `Проверено` в тестовом чате.
- Создан backup через `backup-db.ps1`.
- Установлен автозапуск и ежедневный backup task.
- `scripts\check-bot-only.ps1` и `scripts\pilot-smoke-test.ps1` проходят.
- Frontend, `cloudflared` и порт `5173` не запущены.

## Полный режим: Mini App на Windows

Для полного режима сначала явно поставьте `DEPLOYMENT_MODE=full`. `start-all.ps1` предупреждает, если переменная имеет другое значение, но не меняет `.env` автоматически.

Самый простой запуск из `C:\Projects\PPRBot`:

```text
start-pprbot.bat
```

Его можно запускать двойным кликом. BAT вызывает:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\start-all.ps1"
```

Скрипт делает:

- проверяет Docker, `.venv\Scripts\python.exe`, `npm.cmd`, `cloudflared` и `.env`;
- запускает PostgreSQL через `docker compose up -d db`;
- ждет `healthy`-состояния контейнера `pprbot-postgres`;
- выполняет миграции `.\.venv\Scripts\python.exe -m alembic upgrade head`;
- открывает отдельные PowerShell-окна для backend, frontend, cloudflared и Telegram bot;
- перед запуском bot runner ждет `http://127.0.0.1:8000/health`;
- сохраняет PID запущенных окон в `.runtime/`.

Ожидаемый вывод в конце:

```text
PPRBot startup summary
PostgreSQL: running
migrations: applied
backend: started ...
backend URL: http://127.0.0.1:8000
frontend: started ...
frontend URL: http://127.0.0.1:5173
cloudflared: started ...
bot: started ...
```

Остановка двойным кликом:

```text
stop-pprbot.bat
```

По умолчанию PostgreSQL не останавливается. Чтобы остановить и БД:

```powershell
.\scripts\stop-all.ps1 -StopDatabase
```

Проверить состояние:

```powershell
.\scripts\status.ps1
```

Ручной запуск скриптов:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\start-all.ps1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\stop-all.ps1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\status.ps1"
```

Скрипты не меняют безопасные флаги автоотправки:

```env
NOTIFICATIONS_AUTO_SEND_ENABLED=false
AUTO_SEND_ALLOW_MASS=false
```

Cloudflare Quick Tunnel выдает временный `trycloudflare.com` URL. После каждого изменения URL нужно обновить `WEBAPP_URL` в `.env` и настройки Mini App в BotFather. Для боевой кнопки карточки Telegram использует `t.me` deep link, но сам Mini App все равно должен быть доступен по актуальному публичному HTTPS `WEBAPP_URL`. В `bot_only` Cloudflare не нужен.

## Быстрый старт backend

```powershell
cd ppr-miniapp
copy .env.example .env
docker compose up -d db
python -m venv .venv
.\.venv\Scripts\activate
pip install -r backend\requirements.txt
$env:PYTHONPATH="backend"
alembic upgrade head
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Если импорт из Excel нужен сразу, сначала сделайте preview, потом apply. В примере используется `data/schedule.xlsx` из `.env` и dev-admin заголовок:

```powershell
$headers = @{ "X-Dev-Telegram-Id"="123456789" }
$preview = Invoke-RestMethod -Method Post http://localhost:8000/api/import/excel/preview `
  -Headers $headers `
  -Body @{ mode="safe" }
$preview.summary

Invoke-RestMethod -Method Post http://localhost:8000/api/import/excel/apply `
  -Headers $headers `
  -Body @{ mode="safe"; preview_id=$preview.preview_id }
```

## API ППР

После импорта текущего `data/schedule.xlsx`:

- `GET /api/ppr/all` — все 272 карточки ППР, включая строки без даты.
- `GET /api/ppr/today` — ППР с датой на текущий день.
- `GET /api/ppr/unverified` — 193 уведомления/ППР, которые требуют проверки.
- `GET /api/ppr/missing-date` — 79 карточек ППР без даты; для них `requires_date: true`.
- `GET /api/dashboard/summary` — счетчики дашборда для `admin` и `checker`.
- `GET /api/ppr` — список ППР с backend-поиском, фильтрами, сортировкой и пагинацией.
- `GET /api/ppr/{id}` — одна карточка ППР.
- `POST /api/ppr` — создать ППР, только `admin`.
- `PATCH /api/ppr/{id}` — редактировать ППР, только `admin`.
- `POST /api/ppr/{id}/archive` — архивировать/отключить ППР, только `admin`.
- `POST /api/ppr/{id}/restore` — восстановить ППР, только `admin`.
- `GET /api/notifications/autosend-preview` — admin-preview уведомлений, которые попали бы в автоотправку сейчас.
- `GET /api/notifications` — admin-список уведомлений с фильтрами `status`, `date_from/date_to`, `ppr_event_id`, `failed_only`, `page/page_size`.
- `POST /api/notifications/{id}/retry` — admin retry: переводит `failed` обратно в `planned`, не обнуляя историю попыток.
- `POST /api/notifications/{id}/mark-sent` — admin вручную подтверждает `delivery_unknown -> sent`, нужен комментарий.
- `POST /api/notifications/{id}/retry-unknown?confirm=true` — admin вручную возвращает `delivery_unknown -> planned`; возможен дубль.
- `GET /api/system/scheduler-status` — admin health автоотправки: heartbeat, due/processing/stale/failed/unknown счетчики.
- `POST /api/import/excel/preview` — admin-preview Excel-импорта без изменения ППР.
- `POST /api/import/excel/apply` — применить ранее проверенный preview по `preview_id`.

Строки без даты сохраняются как карточки, но уведомления для них не создаются.

`GET /api/ppr` поддерживает параметры:

- `search` — поиск по названию, проекту, активностям, ответственным, комментарию и Telegram-именам взявшего/проверившего;
- `status`, `project`, `date_from`, `date_to`, `date_state=present|missing`, `checker`, `notify=true|false`, `outlook=true|false`, `include_archived=true`;
- `quick_filter=today|unverified|missing_date|overdue|mine_in_progress|notification_errors`;
- `sort=date_asc|date_desc|overdue_first|updated_desc|title|project`;
- `page`, `page_size` с максимумом `100`.

`overdue` определен так: ППР активна, дата и время выхода уже прошли, статус не `verified`, не `archived` и не `cancelled`.

Пример:

```powershell
Invoke-RestMethod "http://localhost:8000/api/ppr?search=иванов&quick_filter=unverified&sort=overdue_first&page=1&page_size=25" `
  -Headers @{ "X-Dev-Telegram-Id"="123456789" }

Invoke-RestMethod http://localhost:8000/api/dashboard/summary `
  -Headers @{ "X-Dev-Telegram-Id"="123456789" }
```

Для фильтров добавлена миграция `20260710_0006_dashboard_indexes.py`: индексы на `ppr_events.date`, `ppr_events.project`, `ppr_notifications.taken_by_id`. `ppr_status` уже был индексирован.

Для надежной доставки добавлена миграция `20260710_0007_notification_delivery_state.py`: `processing_started_at`, `processing_by`, `sent_at`, `attempt_count`, `last_attempt_at`, `last_error`, `telegram_edit_last_error`, `reminder_count`, `last_reminder_at`.

Для восстановления зависших отправок добавлена миграция `20260710_0008_processing_recovery.py`: `processing_phase` и таблица `scheduler_heartbeats`.

`GET /api/ppr/all` по умолчанию скрывает архивные карточки. Для просмотра архива используйте:

```powershell
Invoke-RestMethod "http://localhost:8000/api/ppr/all?include_archived=true" -Headers @{ "X-Dev-Telegram-Id"="123456789" }
```

## Безопасный импорт Excel

Текущая схема сопоставления Excel -> ППР:

- если в Excel есть колонка `ID`, используется `external_id=<ID>` и `source_key=id:<ID>`;
- если `ID` нет, но есть стабильное значение `Исходная строка`, используется `source_key=source:<значение>`;
- если стабильного ID/source нет, используется `source_key=fingerprint:<sha256>` по нормализованным бизнес-полям: `Название ППР`, `Проект`, `Тип уведомления`, `Активности`, ответственные и `Ссылка`;
- в fingerprint не входят номер строки Excel, дата и время, поэтому перестановка строк и перенос даты не создают новую ППР;
- существующие legacy-записи `source_key=row:<номер>` сопоставляются fallback-логикой; при однозначном match apply мигрирует их на новый стабильный `source_key`;
- если fingerprint неоднозначен или повторяется внутри Excel, preview показывает `duplicate/ambiguous`, а apply эту строку не применяет автоматически;
- ручные карточки Mini App создаются без `source_key`, поэтому не считаются пропавшими из Excel;
- обычный `safe`-импорт не перетирает `is_manually_edited=true`.

Миграция `20260710_0005_import_preview.py` добавляет `ppr_events.source_key` и таблицу `import_runs`. Дополнительная миграция для fingerprint-стратегии не нужна: используется уже существующее поле `source_key`.

Диагностика и backfill существующей БД:

```powershell
$env:PYTHONPATH="backend"
.\.venv\Scripts\python.exe scripts\source-key-audit.py --excel data\schedule.xlsx
.\.venv\Scripts\python.exe scripts\backfill-source-keys.py --preview --excel data\schedule.xlsx
```

Apply меняет только `ppr_events.source_key`, не трогает бизнес-данные, статусы и уведомления. Запускайте его только после проверки preview:

```powershell
$env:PYTHONPATH="backend"
.\.venv\Scripts\python.exe scripts\backfill-source-keys.py --apply --confirm --excel data\schedule.xlsx
```

Отчеты preview/apply сохраняются в `.runtime/source-key-backfill-*.json`.

Режимы:

- `safe` — обновляет только обычные импортированные ППР, ручные правки пропускает.
- `new_only` — создает только новые ППР, существующие не меняет.
- `force` — может перезаписать ручные изменения, требует `confirm_force=true`.

Preview:

```powershell
$headers = @{ "X-Dev-Telegram-Id"="123456789" }
$preview = Invoke-RestMethod -Method Post http://localhost:8000/api/import/excel/preview `
  -Headers $headers `
  -Body @{ mode="safe" }
$preview.summary
$preview.details | Select-Object -First 10
```

Apply:

```powershell
Invoke-RestMethod -Method Post http://localhost:8000/api/import/excel/apply `
  -Headers $headers `
  -Body @{ mode="safe"; preview_id=$preview.preview_id }
```

Если выбран файл через Mini App, apply должен отправить тот же файл. Backend сверяет SHA-256 hash; если файл изменился после preview, вернет `409 Conflict`. Если тот же файл уже успешно импортирован, preview покажет warning, а apply без `force` вернет `409 Conflict`.

Пример preview response:

```json
{
  "preview_id": "a1b2c3",
  "mode": "safe",
  "summary": {
    "total_rows": 272,
    "valid_rows": 272,
    "invalid_rows": 0,
    "new_events": 0,
    "updated_events": 3,
    "unchanged_events": 260,
    "skipped_manual_events": 9,
    "duplicate_rows": 0,
    "missing_date_rows": 79,
    "notifications_to_create": 0,
    "notifications_to_update": 3,
    "notifications_to_skip": 269,
    "events_missing_from_excel": 0,
    "errors_count": 0
  },
  "details": [
    {
      "excel_row_number": 12,
      "external_id": "AUTO-0d1c...",
      "source_key": "fingerprint:0d1c...",
      "title": "ППР ...",
      "action": "source_key_migration",
      "key_method": "fingerprint",
      "match_method": "legacy_row",
      "confidence": "medium",
      "reason": "source_key будет мигрирован на стабильный source_key",
      "fields_changed": {
        "source_key": { "old": "row:12", "new": "fingerprint:0d1c..." }
      },
      "notification_action": "skip"
    }
  ]
}
```

В Mini App admin видит вкладку `Импорт`: выбрать Excel-файл, выбрать режим, нажать `Предпросмотр`, проверить summary/details и только потом `Применить импорт`. Checker вкладку не видит и получает `403 Forbidden` при прямом вызове API.

## Запуск Telegram bot polling

В отдельном окне:

```powershell
cd ppr-miniapp
.\.venv\Scripts\activate
$env:PYTHONPATH="backend"
python -m app.bot.runner
```

## Telegram Bot

1. Создайте бота через [@BotFather](https://t.me/BotFather): команда `/newbot`, затем сохраните токен.
2. Добавьте бота в тестовый чат или группу.
3. Узнайте `chat_id`:
   - временно запустите бота с заполненным `TELEGRAM_BOT_TOKEN`;
   - напишите боту или в группу любое сообщение;
   - откройте `https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getUpdates`;
   - возьмите `message.chat.id` из ответа. Для групп это обычно отрицательное число.
4. Заполните `.env`:

```env
TELEGRAM_ENABLED=true
NOTIFICATIONS_AUTO_SEND_ENABLED=false
PILOT_AUTO_SEND_ALLOWED=false
AUTO_SEND_MASS_LIMIT=10
AUTO_SEND_ALLOW_MASS=false
TELEGRAM_BOT_TOKEN=123456:real-token
TELEGRAM_CHAT_ID=-1001234567890
TELEGRAM_BOT_USERNAME=pprsendbot
TELEGRAM_MINIAPP_SHORT_NAME=
WEBAPP_URL=https://your-mini-app-url.example
ADMIN_TELEGRAM_IDS=123456789,987654321
```

В `DEPLOYMENT_MODE=full` `WEBAPP_URL` остается адресом самой Mini App. Кнопка `Открыть карточку` в Telegram ведет не напрямую на `WEBAPP_URL`, а через Telegram deep link `https://t.me/...`, чтобы Mini App открылась внутри Telegram и получила `initData`. В `bot_only` эта кнопка и deep link намеренно не создаются.

5. Запустите bot runner:

```powershell
cd ppr-miniapp
.\.venv\Scripts\activate
$env:PYTHONPATH="backend"
python -m app.bot.runner
```

Команды бота:

- `/ping` — проверка, бот отвечает `pong`.
- `/help` — список основных команд.
- `/today` — список ППР на сегодня.
- `/dryrun` — показывает до 10 ближайших плановых уведомлений, у которых `scheduled_at <= now`, и общий счетчик без отправки сообщений и без изменения статусов.
- `/planned` — показывает ближайшие 10 `planned`-уведомлений независимо от даты.
- `/autosend_preview` — показывает due-уведомления, которые попали бы в автоотправку, и статус защиты от массовой отправки.
- `/sendtest` — отправляет одно ближайшее `planned`-уведомление в текущий чат и переводит его в `sent`.
- `/sendtest <notification_id>` — отправляет конкретное уведомление.
- `/outlooktest <notification_id>` — вручную ищет событие Outlook Calendar по названию и дате ППР, сохраняет `webLink` в БД.
- `/setoutlook <notification_id> <url>` — dev/test-команда для локальной проверки отображения Outlook-ссылки без Graph. Доступна только при `ENV=development` или `DEV_COMMANDS_ENABLED=true`.
- `/reset_test_statuses` — dev/test-команда для сброса `sent`, `in_progress`, `checked`, `error` обратно в `planned`. Доступна только при `DEV_COMMANDS_ENABLED=true`.
- `/status` — показывает счетчики карточек ППР, общее число уведомлений и разбивку по всем статусам из БД.
- `/users` — список пользователей; только active `admin`.
- `/user_add <telegram_id> <admin|checker> [имя]` — добавить пользователя; только active `admin`.
- `/user_role <telegram_id> <admin|checker>` — изменить роль; только active `admin`.
- `/user_enable <telegram_id>` и `/user_disable <telegram_id>` — включить или отключить пользователя; только active `admin`.

В Telegram-only управлении нельзя отключить самого себя без отдельного подтверждения. Пользователей из `ADMIN_TELEGRAM_IDS` нельзя понизить до `checker` или отключить. Изменения пользователей записываются в `audit_log`; checker получает `Нет доступа`.

`TELEGRAM_ENABLED` отвечает за работу бота и ручные команды. `NOTIFICATIONS_AUTO_SEND_ENABLED` отвечает за массовую автоматическую отправку просроченных уведомлений. Держите `NOTIFICATIONS_AUTO_SEND_ENABLED=false`, пока не проверите бота через `/ping`, `/dryrun`, `/autosend_preview` и `/sendtest`.

`SCHEDULE_AUTO_IMPORT_ENABLED` управляет периодическим импортом `schedule.xlsx` в FastAPI scheduler. По умолчанию флаг выключен (`false`), поэтому Excel не импортируется автоматически каждые 10 минут. Импорт можно выполнять вручную через предусмотренные API/CLI-команды после проверки файла.

При `TELEGRAM_ENABLED=true` и `NOTIFICATIONS_AUTO_SEND_ENABLED=true` bot runner запускает отдельный async-loop автоотправки. Интервал задается `AUTO_SEND_POLL_INTERVAL_SECONDS`. Loop не блокирует aiogram polling. Уведомление перед отправкой атомарно захватывается переходом `planned -> processing`, поэтому второй runner не должен отправить тот же notification повторно. После успешной отправки статус становится `sent`, сохраняются `telegram_chat_id`, `telegram_message_id`, `sent_at`.

Автоотправка обрабатывает только `planned` уведомления, у которых `auto_send_enabled=true`, ППР активна, notify включен, есть дата/время и `scheduled_at <= now`. Если due-уведомлений больше `AUTO_SEND_MASS_LIMIT`, а `AUTO_SEND_ALLOW_MASS=false`, автоотправка блокируется полностью и ничего не отправляет. Если уведомление старше `AUTO_SEND_MAX_LATE_MINUTES`, оно автоматически переводится в `skipped` с причиной `too_late`; ручная `/sendtest <id>` остается доступна.

Ошибки Telegram сохраняются в `last_error`, попытки считаются в `attempt_count`. Сетевые ошибки повторяются до `AUTO_SEND_MAX_ATTEMPTS` с паузой `AUTO_SEND_RETRY_DELAY_SECONDS`; ошибки прав, токена, chat_id или некорректного запроса не ретраятся бесконечно и переходят в `failed`.

Если worker упал в статусе `processing`, scheduler смотрит на `PROCESSING_STALE_AFTER_SECONDS`. Если фаза была `claimed`, то Telegram-запрос еще не начинался и notification безопасно возвращается в `planned`. Если фаза была `sending` или неизвестна, результат доставки считается неопределенным: notification переводится в `delivery_unknown`, автоматически не отправляется повторно и требует ручного решения admin во вкладке `Ошибки`.

## Пилотный запуск

Локальный запуск backend:

```powershell
docker compose up -d db
.\.venv\Scripts\activate
$env:PYTHONPATH="backend"
alembic upgrade head
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Импорт Excel через безопасный preview/apply:

```powershell
$headers = @{ "X-Dev-Telegram-Id"="123456789" }
$preview = Invoke-RestMethod -Method Post http://localhost:8000/api/import/excel/preview `
  -Headers $headers `
  -Body @{ mode="safe" }
$preview.summary
Invoke-RestMethod -Method Post http://localhost:8000/api/import/excel/apply `
  -Headers $headers `
  -Body @{ mode="safe"; preview_id=$preview.preview_id }
```

Локальный запуск frontend:

```powershell
cd frontend
npm install
npm run dev -- --host 127.0.0.1 --port 5173
```

Публичный HTTPS URL для Telegram можно получить через Cloudflare Quick Tunnel. Cloudflare описывает Quick Tunnels как режим для тестирования и разработки: `cloudflared` проксирует локальный web server на случайный публичный `trycloudflare.com` URL.

```powershell
cloudflared tunnel --url http://localhost:5173
```

Скопируйте выданный HTTPS URL в `.env`:

```env
WEBAPP_URL=https://<random>.trycloudflare.com
```

Безопасные настройки Telegram-теста:

```env
TELEGRAM_ENABLED=true
NOTIFICATIONS_AUTO_SEND_ENABLED=false
PILOT_AUTO_SEND_ALLOWED=false
AUTO_SEND_POLL_INTERVAL_SECONDS=30
AUTO_SEND_MASS_LIMIT=10
AUTO_SEND_ALLOW_MASS=false
AUTO_SEND_MAX_ATTEMPTS=3
AUTO_SEND_RETRY_DELAY_SECONDS=60
AUTO_SEND_MAX_LATE_MINUTES=60
PROCESSING_STALE_AFTER_SECONDS=300
DEV_COMMANDS_ENABLED=false
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
TELEGRAM_BOT_USERNAME=pprsendbot
TELEGRAM_MINIAPP_SHORT_NAME=
WEBAPP_URL=https://<public-url>
```

Запуск bot runner:

```powershell
$env:PYTHONPATH="backend"
python -m app.bot.runner
```

Порядок проверки в тестовом чате:

1. `/ping`
2. `/status`
3. `/planned`
4. `/dryrun`
5. `/autosend_preview`
6. `/sendtest <planned_id>`
7. Нажать `Открыть карточку` и проверить нужную карточку Mini App.
8. Проверить `Взять в работу` и `Проверено`.

Как не заспамить чат:

- Держите `NOTIFICATIONS_AUTO_SEND_ENABLED=false` во время пилота.
- Используйте только `/sendtest <planned_id>` для одиночной отправки.
- Перед включением автоотправки выполните `/autosend_preview` и проверьте первые due-уведомления.
- Не ставьте `AUTO_SEND_ALLOW_MASS=true`, пока список due-уведомлений не проверен.
- Старые уведомления старше `AUTO_SEND_MAX_LATE_MINUTES` будут автоматически помечены `skipped: too_late`, а не отправлены массово после простоя.
- Failed-уведомления смотрите в Mini App во вкладке `Ошибки`; admin может вернуть их в `planned` кнопкой `Повторить отправку`.
- `delivery_unknown` смотрите там же: admin либо отмечает “Сообщение уже отправлено”, либо подтверждает повторную отправку с риском дубля.
- Перед повторным локальным тестом можно временно поставить `DEV_COMMANDS_ENABLED=true`, выполнить `/reset_test_statuses`, затем вернуть `DEV_COMMANDS_ENABLED=false`.
- Не включайте автоотправку, пока `/status`, `/planned`, `/autosend_preview`, `/sendtest` и открытие карточки не проверены.

Автоотправка включается только после проверки:

```env
NOTIFICATIONS_AUTO_SEND_ENABLED=true
PILOT_AUTO_SEND_ALLOWED=true
AUTO_SEND_POLL_INTERVAL_SECONDS=30
AUTO_SEND_MASS_LIMIT=10
AUTO_SEND_ALLOW_MASS=false
AUTO_SEND_MAX_ATTEMPTS=3
AUTO_SEND_RETRY_DELAY_SECONDS=60
AUTO_SEND_MAX_LATE_MINUTES=60
PROCESSING_STALE_AFTER_SECONDS=300
```

При включенном `NOTIFICATIONS_AUTO_SEND_ENABLED=true` запуск разрешен только при `PILOT_AUTO_SEND_ALLOWED=true`. Скрипт выводит предупреждение `Pilot auto-send is enabled. Messages will be sent to the configured Telegram chat.` и bot runner пишет warning `AUTO SEND ENABLED: due notifications will be sent automatically`. `AUTO_SEND_ALLOW_MASS` должен оставаться `false`, а `AUTO_SEND_MASS_LIMIT` должен быть от 1 до 10. Если `PILOT_AUTO_SEND_ALLOWED=false`, запуск блокируется. Если автоотправка выключена, `PILOT_AUTO_SEND_ALLOWED` не влияет на запуск.

## Быстрый старт frontend

```powershell
cd ppr-miniapp\frontend
npm install
npm run dev
```

## Важные переменные .env

```env
TELEGRAM_ENABLED=false
NOTIFICATIONS_AUTO_SEND_ENABLED=false
PILOT_AUTO_SEND_ALLOWED=false
AUTO_SEND_POLL_INTERVAL_SECONDS=30
AUTO_SEND_MASS_LIMIT=10
AUTO_SEND_ALLOW_MASS=false
AUTO_SEND_MAX_ATTEMPTS=3
AUTO_SEND_RETRY_DELAY_SECONDS=60
AUTO_SEND_MAX_LATE_MINUTES=60
PROCESSING_STALE_AFTER_SECONDS=300
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_BOT_USERNAME=pprsendbot
TELEGRAM_MINIAPP_SHORT_NAME=
WEBAPP_URL=
ADMIN_TELEGRAM_IDS=
TELEGRAM_WEBAPP_AUTH_MAX_AGE_SECONDS=86400
ENV=development
DEV_COMMANDS_ENABLED=false
DATABASE_URL=postgresql+psycopg://ppr_user:ppr_password@localhost:5432/ppr_db
SCHEDULE_XLSX_PATH=./data/schedule.xlsx
DEFAULT_TIMEZONE=Europe/Moscow
```

Если `TELEGRAM_ENABLED=false`, бот и планировщик не пытаются отправлять Telegram-уведомления и не меняют статусы уведомлений. Если `TELEGRAM_ENABLED=true`, обязательно заполните `TELEGRAM_BOT_TOKEN`. Для автоотправки также заполните `TELEGRAM_CHAT_ID`, поставьте `NOTIFICATIONS_AUTO_SEND_ENABLED=true` и явно разрешите тестовый пилот через `PILOT_AUTO_SEND_ALLOWED=true` после проверки `/autosend_preview` и `/sendtest`.

Проверка preview через API:

```powershell
Invoke-RestMethod "http://localhost:8000/api/notifications/autosend-preview?respect_global_enabled=false" `
  -Headers @{ "X-Dev-Telegram-Id"="123456789" }
```

Добавьте `respect_global_enabled=true`, если нужно увидеть результат с учетом `NOTIFICATIONS_AUTO_SEND_ENABLED`.

`WEBAPP_URL` может быть локальным `http://127.0.0.1:5173` для dev-проверки frontend, но боевую кнопку карточки нельзя вести напрямую на Cloudflare/WEBAPP URL: обычный браузер не даст `window.Telegram.WebApp.initData`. Telegram-сообщение создает обычную `url`-кнопку с deep link:

```text
https://t.me/<TELEGRAM_BOT_USERNAME>?startapp=notification_<id>
```

Если у Mini App задан short name:

```text
https://t.me/<TELEGRAM_BOT_USERNAME>/<TELEGRAM_MINIAPP_SHORT_NAME>?startapp=notification_<id>
```

`TELEGRAM_MINIAPP_SHORT_NAME` можно оставить пустым, если Mini App открывается через `startapp` самого бота.

## Telegram WebApp авторизация

Mini App берет `window.Telegram.WebApp.initData` и отправляет его во все backend-запросы в заголовке:

```http
X-Telegram-Init-Data: <initData>
```

Backend проверяет подпись `initData` через `TELEGRAM_BOT_TOKEN`, проверяет `auth_date` и отклоняет устаревшие данные старше:

```env
TELEGRAM_WEBAPP_AUTH_MAX_AGE_SECONDS=86400
```

Карточка из Telegram открывается через `startapp=notification_<id>`. Frontend берет id из `window.Telegram.WebApp.initDataUnsafe.start_param` или `tgWebAppStartParam`. Старый query param `?notification_id=<id>` используется только для локального dev-режима.

Если `telegram_id` есть в `ADMIN_TELEGRAM_IDS`, пользователь создается или обновляется как `admin`. Если пользователь уже есть в `app_users` и `is_active=true`, он получает доступ со своей ролью. Неизвестный пользователь, которого нет в `ADMIN_TELEGRAM_IDS`, получает `403 Forbidden`.

Dev-заголовки работают только при:

```env
DEV_COMMANDS_ENABLED=true
```

Если `DEV_COMMANDS_ENABLED=false`, backend полностью игнорирует `X-Dev-Telegram-Id`, `X-Dev-Username` и `X-Dev-Full-Name`. Перед пилотом держите `DEV_COMMANDS_ENABLED=false`.

Для локальной проверки frontend без Telegram можно запустить Vite с dev-пользователем:

```powershell
$env:VITE_DEV_TELEGRAM_ID="123456789"
$env:VITE_DEV_USERNAME="admin"
$env:VITE_DEV_FULL_NAME="Admin User"
npm run dev -- --host 127.0.0.1 --port 5173
```

Backend при этом тоже должен быть запущен с `DEV_COMMANDS_ENABLED=true`. В боевом Telegram Mini App эти `VITE_DEV_*` переменные не нужны: используется только `initData`.

Проверка `/api/me` в dev:

```powershell
Invoke-RestMethod http://localhost:8000/api/me -Headers @{
  "X-Dev-Telegram-Id"="123456789"
  "X-Dev-Username"="admin"
  "X-Dev-Full-Name"="Admin User"
}
```

Проверка, что dev-заголовки отключены:

```powershell
# Запустите backend с DEV_COMMANDS_ENABLED=false
Invoke-RestMethod http://localhost:8000/api/me -Headers @{ "X-Dev-Telegram-Id"="123456789" }
```

Ожидаемый результат: `401`, потому что без Telegram `initData` dev-заголовки не принимаются.

Проверка из Telegram Mini App:

1. Укажите публичный HTTPS `WEBAPP_URL`.
2. Укажите реальный `TELEGRAM_BOT_TOKEN`.
3. Укажите `TELEGRAM_BOT_USERNAME` без `@`.
4. Если Mini App создана с short name, укажите `TELEGRAM_MINIAPP_SHORT_NAME`.
5. Добавьте свой Telegram id в `ADMIN_TELEGRAM_IDS` или создайте пользователя через `/api/users`.
6. Выполните `/sendtest <planned_id>`.
7. Кнопка `Открыть карточку` должна вести на `https://t.me/...startapp=notification_<id>`.
8. Откройте карточку из Telegram-кнопки.
9. `/api/me` должен пройти через `initData`, а Mini App должна открыть нужную карточку.

Прямое открытие Cloudflare URL в Chrome без Telegram должно показывать `Требуется Telegram`.

## Пользователи и роли

Доступ к Mini App и управляющим действиям идет через Telegram WebApp `initData`. Первичные админы задаются в `.env`:

```env
ADMIN_TELEGRAM_IDS=123456789,987654321
```

Роли:

- `admin` — просмотр, будущие CRUD-действия с ППР, управление пользователями, ручная Outlook-синхронизация.
- `checker` — просмотр карточек, взять в работу, проверить, комментарии и история.

Если пользователь не входит в `ADMIN_TELEGRAM_IDS` и его нет в таблице `app_users`, API вернет `403 Forbidden`. Такой пользователь не получает права автоматически.

Новые endpoints:

- `GET /api/me` — текущий пользователь и роль.
- `GET /api/users` — только `admin`.
- `POST /api/users` — только `admin`.
- `PATCH /api/users/{id}` — только `admin`.

Для прямой локальной проверки API без Telegram `initData` можно временно включить:

```env
DEV_COMMANDS_ENABLED=true
```

И передавать dev-заголовки:

```powershell
Invoke-RestMethod http://localhost:8000/api/me -Headers @{
  "X-Dev-Telegram-Id"="123456789"
  "X-Dev-Username"="admin"
  "X-Dev-Full-Name"="Admin User"
}
```

Проверка прав:

```powershell
# admin из ADMIN_TELEGRAM_IDS автоматически создается/обновляется
Invoke-RestMethod http://localhost:8000/api/me -Headers @{ "X-Dev-Telegram-Id"="123456789" }

# admin добавляет checker
Invoke-RestMethod -Method Post http://localhost:8000/api/users `
  -Headers @{ "X-Dev-Telegram-Id"="123456789" } `
  -ContentType "application/json" `
  -Body '{"telegram_id":"222222222","username":"checker","full_name":"Checker User","role":"checker","is_active":true}'

# checker может смотреть карточки
Invoke-RestMethod http://localhost:8000/api/ppr/all -Headers @{ "X-Dev-Telegram-Id"="222222222" }

# неизвестный пользователь получает 403
Invoke-RestMethod http://localhost:8000/api/me -Headers @{ "X-Dev-Telegram-Id"="333333333" }
```

В Telegram-кнопках `Взять в работу` и `Проверено` также проверяется `app_users`: активный `admin` или `checker` может выполнить действие, неизвестный или отключенный пользователь получает alert `Нет доступа`.

### Управление пользователями в Mini App

Admin видит вкладку `Пользователи` в Mini App. Checker эту вкладку не видит.

Как добавить checker:

1. Откройте Mini App под пользователем с ролью `admin`.
2. Перейдите во вкладку `Пользователи`.
3. Нажмите `Добавить пользователя`.
4. Заполните `Telegram ID`, при необходимости `username` и `full_name`.
5. Выберите роль `checker`, оставьте `Пользователь активен` включенным.
6. Нажмите `Добавить`.

Как отключить пользователя:

1. Во вкладке `Пользователи` найдите пользователя.
2. Нажмите `Отключить`.
3. У пользователя появится статус `Отключен`, а API начнет возвращать ему `403 Forbidden`.

Кнопка `Редактировать` позволяет менять `username`, `full_name`, роль и активность. Неизвестные пользователи, которых нет в `ADMIN_TELEGRAM_IDS` и `app_users`, получают `403 Forbidden` и не видят списки ППР.

## Управление ППР из Mini App

Только `admin` может создавать, редактировать, архивировать и восстанавливать ППР. `checker` видит карточки, историю и рабочие кнопки проверки, но не видит админские кнопки и получает `403 Forbidden` при прямом вызове admin endpoints.

Поля формы:

- название ППР;
- проект;
- дата выхода;
- время выхода;
- активности / описание работ;
- отправлять уведомление;
- Outlook-ссылка;
- комментарий.

При создании или редактировании ППР backend синхронизирует start-уведомление:

- есть дата, время, ППР активна и `notify=true` — создается/обновляется notification;
- дата убрана, `notify=false` или ППР архивирована — notification переводится в `skipped`;
- дата/время в прошлом — notification остается `planned`, но `auto_send_enabled=false`, поэтому массовая автоотправка его не подхватит. Для теста используйте ручной `/sendtest <notification_id>`.

Ручные изменения защищены от обычного Excel-импорта: карточка получает `is_manually_edited=true` и `manual_updated_at`, после чего `safe`-импорт не перезаписывает ее название, дату, время и прочие управляемые поля. Для принудительной перезаписи используйте Excel-импорт в режиме `force` после preview и отдельного подтверждения.

Создать ППР через API:

```powershell
Invoke-RestMethod -Method Post http://localhost:8000/api/ppr `
  -Headers @{ "X-Dev-Telegram-Id"="123456789" } `
  -ContentType "application/json" `
  -Body '{"title":"Тестовая ППР","project":"Pilot","date":"2026-07-20","start_time":"09:30","activities":"Описание работ","notify":true,"outlook_link":"https://example.com/outlook","comment":"created manually"}'
```

Изменить дату ППР:

```powershell
Invoke-RestMethod -Method Patch http://localhost:8000/api/ppr/273 `
  -Headers @{ "X-Dev-Telegram-Id"="123456789" } `
  -ContentType "application/json" `
  -Body '{"date":"2026-07-21","start_time":"10:15"}'
```

Архивировать и восстановить:

```powershell
Invoke-RestMethod -Method Post http://localhost:8000/api/ppr/273/archive -Headers @{ "X-Dev-Telegram-Id"="123456789" }
Invoke-RestMethod -Method Post http://localhost:8000/api/ppr/273/restore -Headers @{ "X-Dev-Telegram-Id"="123456789" }
```

Проверить, что checker не может редактировать:

```powershell
Invoke-RestMethod -Method Patch http://localhost:8000/api/ppr/273 `
  -Headers @{ "X-Dev-Telegram-Id"="222222222" } `
  -ContentType "application/json" `
  -Body '{"title":"Forbidden update"}'
```

Ожидаемый результат — `403 Forbidden`.

## Outlook Graph

Outlook по умолчанию выключен. Массовая синхронизация не запускается автоматически; проверка выполняется вручную через Telegram-команду или API.

Для включения поиска события Outlook Calendar:

```env
OUTLOOK_ENABLED=true
OUTLOOK_TENANT_ID=<azure-tenant-id>
OUTLOOK_CLIENT_ID=<app-registration-client-id>
OUTLOOK_CLIENT_SECRET=<client-secret-value>
OUTLOOK_USER_ID=calendar-owner@example.com
OUTLOOK_SEARCH_DAYS_WINDOW=1
```

Что нужно в Azure/Microsoft 365:

- App registration в нужном tenant.
- Client secret для этой app registration.
- Microsoft Graph Application permission `Calendars.Read`.
- Admin consent на это permission.
- Почтовый ящик/пользователь, чей календарь читаем: `OUTLOOK_USER_ID`.
- Если в организации доступ приложений к Exchange ограничен, админы должны разрешить этой app registration доступ к нужному mailbox/calendar.

Логика:

1. Команда `/outlooktest <notification_id>` или API `POST /api/outlook/sync/{notification_id}` берет название и дату ППР из БД.
2. Получает Graph access token через client credentials.
3. Ищет события календаря пользователя `OUTLOOK_USER_ID` через `calendarView` за дату ППР.
4. Сравнивает `subject` с названием ППР.
5. Если совпадение найдено, сохраняет `webLink` в БД как `outlook_link` / `outlook_url`.

Проверка:

```powershell
Invoke-RestMethod -Method Post http://localhost:8000/api/outlook/sync/87
```

Или в Telegram:

```text
/outlooktest 87
```

После сохранения ссылки новые Telegram-уведомления по этой ППР будут показывать ссылку Outlook в тексте сообщения.

Локальная проверка отображения без Graph:

```text
/setoutlook 87 https://example.com/test
/sendtest 87
```

В Telegram-сообщении должна появиться строка `📅 Outlook: открыть событие`. В Mini App карточке та же ссылка отображается только если `outlook_link` / `outlook_url` уже сохранен в БД.

## Текущий статус

Это стартовый каркас разработки. Уже заложены:

- модели БД;
- импорт Excel;
- API миниапки;
- Telegram-кнопки;
- базовая mini app;
- заготовка Outlook Graph.

Дальше Codex нужно давать задачи точечно: исправить запуск, добавить тесты, улучшить UI, довести Graph-доступы, настроить деплой.
