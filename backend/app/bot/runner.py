import asyncio
import logging
import os
import socket
import sys
from datetime import datetime
from html import escape
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import uuid4

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import CallbackQuery, ErrorEvent, Message

from app.bot.keyboards import is_card_deep_link_configured, notification_keyboard
from app.config import get_settings
from app.db.models import AppUser
from app.db.session import SessionLocal
from app.services.outlook_graph import (
    OutlookGraphConfigError,
    OutlookGraphDisabledError,
    OutlookGraphNoDateError,
    OutlookGraphRequestError,
    sync_notification_outlook_link,
)
from app.services.ppr_service import check_notification, get_notification, requeue_notification, take_notification
from app.services.telegram_sender import (
    build_autosend_preview,
    get_due_notifications,
    get_next_planned_notification,
    get_notification_for_sending,
    get_planned_notifications,
    list_notification_history,
    get_status_counts,
    get_today_notifications,
    list_notifications,
    mark_scheduler_poll_finished,
    mark_scheduler_poll_started,
    mark_scheduler_started,
    mark_scheduler_stopped,
    render_notification_brief,
    render_notification_details,
    render_notification_message,
    retry_failed_notification,
    reset_test_notification_statuses,
    send_notification,
    send_due_notifications,
    telegram_sending_available,
)
from app.services.user_service import (
    ROLE_ADMIN,
    ROLE_CHECKER,
    create_user,
    assign_checker_from_reply,
    get_or_sync_user,
    is_active_admin_user,
    update_user,
    user_display,
)

settings = get_settings()
dp = Dispatcher()
logger = logging.getLogger(__name__)
WORKER_ID = f"bot:{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        text = record.getMessage()
        for secret in (get_settings().telegram_bot_token, get_settings().outlook_client_secret):
            if secret:
                text = text.replace(secret, "[REDACTED]")
        record.msg = text
        record.args = ()
        return True


def configure_logging(log_dir: Path | None = None) -> None:
    if log_dir is None:
        project_root = Path(__file__).resolve().parents[3]
        log_dir = project_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    error_handler = RotatingFileHandler(
        log_dir / "errors.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.addFilter(SecretRedactionFilter())
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.addFilter(SecretRedactionFilter())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[stream_handler, error_handler],
        force=True,
    )


@dp.errors()
async def on_unhandled_error(event: ErrorEvent):
    logger.exception(
        "Unhandled Telegram update failed: %s",
        event.exception,
        exc_info=(type(event.exception), event.exception, event.exception.__traceback__),
    )
    update = event.update
    message = getattr(update, "message", None)
    if message is None:
        callback = getattr(update, "callback_query", None)
        message = getattr(callback, "message", None)
    if message is not None:
        try:
            await message.answer(
                "Не удалось выполнить команду. Ошибка записана в журнал.",
                parse_mode=None,
            )
        except Exception:
            logger.exception("Failed to send Telegram error response.")


def build_notification_list(title: str, notifications) -> str:
    if not notifications:
        return f"{title}\n\nНет ППР."

    lines = [title, ""]
    for idx, notif in enumerate(notifications, start=1):
        lines.append(f"{idx}. {render_notification_brief(notif)}")
    return "\n".join(lines)


def parse_notification_id(message: Message) -> tuple[bool, int | None]:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 1:
        return False, None
    try:
        return True, int(parts[1].strip())
    except ValueError:
        return True, None


def parse_history_args(message: Message) -> tuple[str | None, int | None, str | None]:
    parts = (message.text or "").split()
    if len(parts) == 1:
        return None, 10, None
    if len(parts) > 3:
        return None, None, "Использование: /history [status] [limit]"
    status = None
    limit = 10
    if len(parts) == 2:
        if parts[1].isdigit():
            limit = int(parts[1])
        else:
            status = parts[1].lower()
    else:
        status = parts[1].lower()
        if not parts[2].isdigit():
            return None, None, "Limit должен быть числом от 1 до 50."
        limit = int(parts[2])
    if limit < 1:
        return None, None, "Limit должен быть числом от 1 до 50."
    return status, min(limit, 50), None


def split_telegram_text(text: str, max_length: int = 4000) -> list[str]:
    chunks = []
    remaining = text
    while len(remaining) > max_length:
        split_at = remaining.rfind("\n", 0, max_length + 1)
        if split_at <= 0:
            split_at = max_length
        entity_start = remaining.rfind("&", 0, split_at)
        if entity_start >= 0 and remaining.find(";", entity_start, split_at + 16) == -1:
            split_at = entity_start
            if split_at <= 0:
                split_at = max_length
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    if remaining or not chunks:
        chunks.append(remaining)
    return chunks


def parse_setoutlook_args(message: Message) -> tuple[int | None, str | None]:
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        return None, None
    try:
        notification_id = int(parts[1])
    except ValueError:
        return None, parts[2].strip()
    return notification_id, parts[2].strip()


def parse_requeue_args(message: Message) -> tuple[int | None, datetime | None, bool, bool]:
    parts = (message.text or "").split()
    if len(parts) < 4 or not parts[1].isdigit():
        return None, None, False, False
    try:
        scheduled_at = datetime.strptime(f"{parts[2]} {parts[3]}", "%Y-%m-%d %H:%M")
    except ValueError:
        return int(parts[1]), None, False, False
    confirm = len(parts) >= 5 and parts[4].upper() == "CONFIRM"
    force = any(part.upper() == "FORCE" for part in parts[5:])
    return int(parts[1]), scheduled_at, confirm, force


def parse_user_add_args(message: Message) -> tuple[str | None, str | None, str | None]:
    parts = (message.text or "").split(maxsplit=3)
    if len(parts) < 3 or not parts[1].isdigit():
        return None, None, None
    return parts[1], parts[2], parts[3].strip() if len(parts) == 4 else None


def parse_user_target_args(message: Message) -> tuple[str | None, str | None]:
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit():
        return None, None
    return parts[1], parts[2].strip() if len(parts) == 3 else None


def get_message_admin(db, message: Message) -> AppUser | None:
    telegram_user = message.from_user
    if telegram_user is None:
        return None
    app_user = get_or_sync_user(
        db,
        str(telegram_user.id),
        telegram_user.username,
        telegram_user_full_name(telegram_user),
    )
    return app_user if is_active_admin_user(app_user) else None


def dev_commands_allowed() -> bool:
    return settings.dev_commands_enabled or settings.env.strip().lower() == "development"


def strict_dev_commands_allowed() -> bool:
    return settings.dev_commands_enabled


def render_planned_notification_line(index: int, notif) -> str:
    event = notif.event
    date_text = event.date.strftime("%d.%m.%Y") if event.date else notif.scheduled_at.strftime("%d.%m.%Y")
    time_text = event.start_time.strftime("%H:%M") if event.start_time else notif.scheduled_at.strftime("%H:%M")
    return f"{index}. #{notif.id} {date_text} {time_text} {escape(event.title)}"


def telegram_user_full_name(user) -> str | None:
    return " ".join(filter(None, [user.first_name, user.last_name])).strip() or None


async def send_due_notifications_job(bot: Bot, worker_id: str = WORKER_ID) -> None:
    if not telegram_sending_available():
        return

    with SessionLocal() as db:
        mark_scheduler_poll_started(db, worker_id)
        try:
            sent = await send_due_notifications(db, bot=bot, worker_id=worker_id)
            mark_scheduler_poll_finished(db, worker_id)
            if sent:
                logger.info("Sent %s Telegram notification(s).", sent)
        except Exception as exc:
            mark_scheduler_poll_finished(db, worker_id, error=exc)
            raise


async def auto_send_loop(bot: Bot) -> None:
    interval = max(1, settings.auto_send_poll_interval_seconds)
    with SessionLocal() as db:
        mark_scheduler_started(db, WORKER_ID)
    logger.info("Auto-send loop started, worker_id=%s interval=%s seconds.", WORKER_ID, interval)
    while True:
        try:
            await send_due_notifications_job(bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Auto-send loop iteration failed: %s", exc)
        await asyncio.sleep(interval)


async def edit_saved_notification_message(bot: Bot, notif) -> None:
    if not notif.telegram_chat_id or not notif.telegram_message_id:
        logger.warning("Cannot edit notification %s: telegram chat/message id is missing.", notif.id)
        return
    try:
        await bot.edit_message_text(
            chat_id=notif.telegram_chat_id,
            message_id=int(notif.telegram_message_id),
            text=render_notification_message(notif),
            reply_markup=notification_keyboard(notif.id, notif.event.ppr_status),
            disable_web_page_preview=True,
        )
        if notif.telegram_edit_last_error:
            with SessionLocal() as db:
                fresh = get_notification(db, notif.id)
                if fresh:
                    fresh.telegram_edit_last_error = None
                    db.commit()
    except Exception as exc:
        logger.warning("Failed to edit Telegram message notification_id=%s: %s", notif.id, exc)
        with SessionLocal() as db:
            fresh = get_notification(db, notif.id)
            if fresh:
                fresh.telegram_edit_last_error = str(exc)[:1000]
                db.commit()


@dp.message(Command("ping"))
async def on_ping(message: Message):
    await message.answer("pong")


@dp.message(Command("help"))
async def on_help(message: Message):
    await message.answer(
        "\n".join(
            [
                "Команды бота",
                "",
                "/ping - проверка связи",
                "/status - счетчики и настройки",
                "/planned - ближайшие planned-уведомления",
                "/history [status] [limit] - история уведомлений (admin)",
                "/dryrun - due-уведомления без отправки",
                "/autosend_preview - preview автоотправки",
                "/sendtest - отправить одно planned-уведомление",
                "/notification - информация об уведомлении (admin)",
                "/requeue - preview/очередь одного уведомления (admin)",
                "/today - ППР на сегодня",
                "/outlooktest - проверить Outlook-ссылку",
                "/users - пользователи (admin)",
                "/user_add - добавить пользователя (admin)",
                "/user_add_reply checker - назначить checker ответом на сообщение (admin)",
                "/user_role - изменить роль (admin)",
                "/user_enable, /user_disable - включить/отключить пользователя (admin)",
                "/help - список команд",
            ]
        )
    )


@dp.message(Command("today"))
async def on_today(message: Message):
    with SessionLocal() as db:
        notifications = get_today_notifications(db)
        await message.answer(build_notification_list("ППР на сегодня", notifications))


@dp.message(Command("users"))
async def on_users(message: Message):
    with SessionLocal() as db:
        admin = get_message_admin(db, message)
        if not admin:
            await message.answer("Нет доступа")
            return
        users = db.query(AppUser).order_by(AppUser.telegram_id.asc()).all()

    lines = ["Пользователи", ""]
    for item in users:
        name = item.full_name or (f"@{item.username}" if item.username else "без имени")
        state = "активен" if item.is_active else "отключен"
        lines.append(f"{item.telegram_id} — {escape(name)} — {item.role} — {state}")
    await message.answer("\n".join(lines) if len(lines) > 2 else "Пользователей нет.")


@dp.message(Command("user_add"))
async def on_user_add(message: Message):
    with SessionLocal() as db:
        admin = get_message_admin(db, message)
        if not admin:
            await message.answer("Нет доступа")
            return
        telegram_id, role, full_name = parse_user_add_args(message)
        if not telegram_id or not role:
            await message.answer("Использование: /user_add &lt;telegram_id&gt; &lt;admin|checker&gt; [имя]")
            return
        try:
            created = create_user(db, telegram_id, None, full_name, role, True, actor=admin)
        except ValueError as exc:
            await message.answer(escape(str(exc)))
            return
    await message.answer(f"Пользователь {created.telegram_id} добавлен: {created.role}.")


@dp.message(Command("user_add_reply"))
async def on_user_add_reply(message: Message):
    parts = (message.text or "").split()
    with SessionLocal() as db:
        admin = get_message_admin(db, message)
        if not admin:
            await message.answer("Нет доступа")
            return
        if len(parts) != 2 or parts[1].lower() != ROLE_CHECKER:
            await message.answer("Ответьте этой командой на сообщение пользователя:\n/user_add_reply checker")
            return
        reply = message.reply_to_message
        if reply is None:
            await message.answer("Ответьте этой командой на сообщение пользователя:\n/user_add_reply checker")
            return
        target = reply.from_user
        if target is None:
            await message.answer("В сообщении нет пользователя Telegram.")
            return
        if getattr(target, "is_bot", False):
            await message.answer("Нельзя назначить Telegram-бота checker.")
            return
        full_name = " ".join(part for part in [target.first_name, target.last_name] if part).strip() or None
        try:
            user = assign_checker_from_reply(
                db,
                admin,
                str(target.id),
                target.username,
                full_name,
            )
        except (PermissionError, ValueError) as exc:
            await message.answer(escape(str(exc)))
            return

    username = f"@{escape(user.username)}" if user.username else "нет"
    response = (
        "Пользователь назначен checker:\n"
        f"Имя: {escape(user.full_name or 'нет')}\n"
        f"Username: {username}\n"
        f"Telegram ID: {escape(user.telegram_id)}"
    )
    if not user.is_active:
        response += f"\nРоль изменена, но пользователь отключён. Используйте /user_enable {escape(user.telegram_id)}."
    await message.answer(response)


@dp.message(Command("user_role"))
async def on_user_role(message: Message):
    with SessionLocal() as db:
        admin = get_message_admin(db, message)
        if not admin:
            await message.answer("Нет доступа")
            return
        telegram_id, role = parse_user_target_args(message)
        if not telegram_id or not role:
            await message.answer("Использование: /user_role &lt;telegram_id&gt; &lt;admin|checker&gt;")
            return
        target = db.query(AppUser).filter(AppUser.telegram_id == telegram_id).one_or_none()
        if not target:
            await message.answer("Пользователь не найден.")
            return
        try:
            updated = update_user(db, target, role=role, actor=admin)
        except ValueError as exc:
            await message.answer(escape(str(exc)))
            return
    await message.answer(f"Роль пользователя {updated.telegram_id}: {updated.role}.")


async def set_user_enabled(message: Message, enabled: bool) -> None:
    with SessionLocal() as db:
        admin = get_message_admin(db, message)
        if not admin:
            await message.answer("Нет доступа")
            return
        telegram_id, extra = parse_user_target_args(message)
        if not telegram_id or extra:
            command = "/user_enable" if enabled else "/user_disable"
            await message.answer(f"Использование: {command} &lt;telegram_id&gt;")
            return
        target = db.query(AppUser).filter(AppUser.telegram_id == telegram_id).one_or_none()
        if not target:
            await message.answer("Пользователь не найден.")
            return
        try:
            updated = update_user(db, target, is_active=enabled, actor=admin)
        except ValueError as exc:
            await message.answer(escape(str(exc)))
            return
    await message.answer(f"Пользователь {updated.telegram_id} {'включен' if updated.is_active else 'отключен'}.")


@dp.message(Command("user_enable"))
async def on_user_enable(message: Message):
    await set_user_enabled(message, True)


@dp.message(Command("user_disable"))
async def on_user_disable(message: Message):
    await set_user_enabled(message, False)


@dp.message(Command("dryrun"))
async def on_dryrun(message: Message):
    with SessionLocal() as db:
        total = len(get_due_notifications(db, limit=10000))
        notifications = get_due_notifications(db, limit=10)
        text = build_notification_list("Dry-run: плановые уведомления к отправке", notifications)
        await message.answer(
            f"{text}\n\n"
            "Показываются только уведомления, у которых scheduled_at ≤ now.\n"
            f"Показано: {len(notifications)} из {total}. Сообщения не отправлены, статусы не изменены."
        )


@dp.message(Command("autosend_preview"))
async def on_autosend_preview(message: Message):
    user = message.from_user
    if user is None:
        await message.answer("Нет доступа")
        return

    with SessionLocal() as db:
        app_user = get_or_sync_user(db, str(user.id), user.username, telegram_user_full_name(user))
        if not app_user or not app_user.is_active or app_user.role != ROLE_ADMIN:
            await message.answer("Нет доступа")
            return
        preview = build_autosend_preview(db, limit=10, respect_global_enabled=True)

    summary = preview["summary"]
    lines = [
        "Autosend preview",
        "",
        f"due notifications: {summary['total_due']}",
        f"global auto-send: {'enabled' if summary['global_auto_send_enabled'] else 'disabled'}",
        f"mass limit: {summary['mass_send_limit']}",
        f"mass send allowed: {'yes' if summary['mass_send_allowed'] else 'no'}",
        f"mass send blocked: {'yes' if summary['mass_send_blocked'] else 'no'}",
        f"would send now: {summary['would_send']}",
    ]
    if summary["blocked_reason"]:
        lines.append(f"blocked reason: {escape(summary['blocked_reason'])}")

    lines.extend(["", "Первые 10 уведомлений:"])
    if not preview["items"]:
        lines.append("Нет due-уведомлений.")
    else:
        for item in preview["items"]:
            project = f" — {escape(item['project'])}" if item["project"] else ""
            lines.append(
                f"#{item['notification_id']} "
                f"{escape(item['planned_datetime'])} "
                f"{escape(item['title'])}{project}"
            )
        if summary["total_due"] > len(preview["items"]):
            lines.append(f"Показаны первые {len(preview['items'])} из {summary['total_due']}.")

    await message.answer("\n".join(lines), disable_web_page_preview=True)


@dp.message(Command("planned"))
async def on_planned(message: Message):
    with SessionLocal() as db:
        notifications = get_planned_notifications(db, limit=10)
        if not notifications:
            await message.answer("Ближайшие planned-уведомления\n\nНет planned-уведомлений.")
            return

        lines = ["Ближайшие planned-уведомления", ""]
        for idx, notif in enumerate(notifications, start=1):
            lines.append(render_planned_notification_line(idx, notif))
        await message.answer("\n".join(lines))


def render_history(notifications, status: str | None, limit: int) -> str:
    title = "История уведомлений"
    if status:
        title += f" — фильтр: {escape(status)}"
    lines = [title, f"Показано: {len(notifications)} (limit {limit})", ""]
    if not notifications:
        lines.append("Записей не найдено.")
    for notif in notifications:
        event = notif.event
        sent_at = notif.sent_at.strftime("%Y-%m-%d %H:%M:%S") if notif.sent_at else "нет"
        lines.extend(
            [
                f"#{notif.id} — {escape(event.title)}",
                f"Проект: {escape(event.project or 'нет')}",
                f"scheduled_at: {escape(notif.scheduled_at.strftime('%Y-%m-%d %H:%M:%S'))}",
                f"PPR status: {escape(event.ppr_status)}; notification status: {escape(notif.status)}",
                f"sent_at: {escape(sent_at)}; auto_send_enabled: {str(notif.auto_send_enabled).lower()}",
                "",
            ]
        )
    lines.extend(
        [
            "Подсказки:",
            "/notification &lt;id&gt;",
            "/requeue &lt;id&gt; &lt;YYYY-MM-DD&gt; &lt;HH:MM&gt;",
        ]
    )
    return "\n".join(lines)


@dp.message(Command("history"))
async def on_history(message: Message):
    with SessionLocal() as db:
        admin = get_message_admin(db, message)
        if not admin:
            await message.answer("Нет доступа")
            return
        status, limit, parse_error = parse_history_args(message)
        if parse_error:
            await message.answer(escape(parse_error))
            return
        try:
            notifications = list_notification_history(db, status=status, limit=limit or 10)
        except ValueError as exc:
            await message.answer(escape(str(exc)))
            return
        text = render_history(notifications, status, limit or 10)

    for chunk in split_telegram_text(text):
        await message.answer(chunk)


@dp.message(Command("sendtest"))
async def on_sendtest(message: Message, bot: Bot):
    has_notification_id, notification_id = parse_notification_id(message)
    if has_notification_id and notification_id is None:
        await message.answer("ID уведомления должен быть числом.")
        return

    with SessionLocal() as db:
        notif = get_notification_for_sending(db, notification_id) if has_notification_id else get_next_planned_notification(db)
        if not notif:
            await message.answer("Уведомление не найдено." if has_notification_id else "Нет planned-уведомлений.")
            return
        if notif.status != "planned":
            await message.answer(f"Уведомление #{notif.id} имеет статус {notif.status}; отправка отменена.")
            return
        auto_send_warning = ""
        if not notif.auto_send_enabled:
            auto_send_warning = "\nВнимание: auto_send_enabled=false, отправка выполнена вручную через /sendtest."
            logger.warning("Manual /sendtest for notification %s with auto_send_enabled=false.", notif.id)

        try:
            sent = await send_notification(db, notif, bot=bot, chat_id=message.chat.id)
        except Exception as exc:
            db.rollback()
            logger.exception("Failed to send test notification %s: %s", notif.id, exc)
            await message.answer(f"Ошибка отправки уведомления #{notif.id}: {escape(str(exc))}")
            return

        if not sent:
            await message.answer("Отправка не выполнена: проверьте TELEGRAM_ENABLED и TELEGRAM_BOT_TOKEN.")
            return
        await message.answer(f"Отправлено одно тестовое уведомление #{notif.id}.{auto_send_warning}")


@dp.message(Command("outlooktest"))
async def on_outlooktest(message: Message):
    has_notification_id, notification_id = parse_notification_id(message)
    if not has_notification_id or notification_id is None:
        await message.answer("Использование: /outlooktest &lt;notification_id&gt;")
        return

    with SessionLocal() as db:
        notif = get_notification(db, notification_id)
        if not notif:
            await message.answer("Уведомление не найдено.")
            return

        try:
            link = await sync_notification_outlook_link(db, notif)
        except OutlookGraphDisabledError:
            await message.answer("Outlook отключен: OUTLOOK_ENABLED=false.")
            return
        except OutlookGraphNoDateError:
            await message.answer(f"У ППР #{notification_id} нет даты, поиск Outlook невозможен.")
            return
        except OutlookGraphConfigError as exc:
            await message.answer(f"Outlook не настроен: {escape(str(exc))}")
            return
        except OutlookGraphRequestError as exc:
            logger.exception("Outlook Graph request failed for notification %s: %s", notification_id, exc)
            await message.answer(f"Ошибка запроса Outlook Graph: {escape(str(exc))}")
            return
        except Exception as exc:
            logger.exception("Outlook sync failed for notification %s: %s", notification_id, exc)
            await message.answer(f"Ошибка Outlook sync: {escape(str(exc))}")
            return

        if not link:
            await message.answer(
                f"Событие Outlook не найдено для уведомления #{notification_id}.\n"
                f"Дата: {notif.event.date.isoformat() if notif.event.date else 'нет даты'}\n"
                f"Название: {escape(notif.event.title)}"
            )
            return

        await message.answer(
            f"Ссылка Outlook сохранена для уведомления #{notification_id}:\n{escape(link, quote=False)}",
            disable_web_page_preview=True,
        )


@dp.message(Command("setoutlook"))
async def on_setoutlook(message: Message):
    if not dev_commands_allowed():
        await message.answer("Команда /setoutlook доступна только при ENV=development или DEV_COMMANDS_ENABLED=true.")
        return

    notification_id, url = parse_setoutlook_args(message)
    if notification_id is None or not url:
        await message.answer("Использование: /setoutlook &lt;notification_id&gt; &lt;url&gt;")
        return
    if not (url.startswith("http://") or url.startswith("https://")):
        await message.answer("URL должен начинаться с http:// или https://.")
        return

    with SessionLocal() as db:
        notif = get_notification(db, notification_id)
        if not notif:
            await message.answer("Уведомление не найдено.")
            return
        notif.event.outlook_link = url
        db.commit()

    await message.answer("ссылка сохранена")


def format_requeue_preview(notif, new_scheduled_at: datetime, *, force: bool) -> str:
    event = notif.event
    archive_note = ""
    if not event.is_active or event.ppr_status in {"archived", "cancelled"}:
        archive_note = "\nВнимание: для архивной/отменённой ППР потребуется CONFIRM FORCE."
    return (
        "Preview requeue\n"
        f"Notification ID: {notif.id}\n"
        f"ППР: {escape(event.title)}\n"
        f"Текущий статус ППР: {escape(event.ppr_status)}\n"
        f"Текущий статус уведомления: {escape(notif.status)}\n"
        f"Прошлое scheduled_at: {escape(notif.scheduled_at.strftime('%Y-%m-%d %H:%M'))}\n"
        f"Новое scheduled_at: {escape(new_scheduled_at.strftime('%Y-%m-%d %H:%M'))}\n"
        f"auto_send_enabled: {str(notif.auto_send_enabled).lower()}\n"
        "После подтверждения запись будет повторно отправлена scheduler."
        f"{archive_note}"
    )


@dp.message(Command("notification"))
async def on_notification(message: Message):
    with SessionLocal() as db:
        admin = get_message_admin(db, message)
        if not admin:
            await message.answer("Нет доступа")
            return
        has_notification_id, notification_id = parse_notification_id(message)
        if not has_notification_id or notification_id is None:
            await message.answer("Использование: /notification &lt;notification_id&gt;")
            return
        notif = get_notification(db, notification_id)
        if not notif:
            await message.answer("Уведомление не найдено.")
            return
        await message.answer(
            "Уведомление\n"
            f"ППР: {escape(notif.event.title)}\n"
            f"PPR status: {escape(notif.event.ppr_status)}\n"
            f"Notification status: {escape(notif.status)}\n"
            f"scheduled_at: {escape(notif.scheduled_at.strftime('%Y-%m-%d %H:%M'))}\n"
            f"auto_send_enabled: {str(notif.auto_send_enabled).lower()}\n"
            f"attempts: {notif.attempt_count}\n"
            f"sent_at: {escape(notif.sent_at.strftime('%Y-%m-%d %H:%M:%S') if notif.sent_at else 'нет')}\n"
            f"last_error: {escape(notif.last_error or 'нет')}"
        )


@dp.message(Command("requeue"))
async def on_requeue(message: Message):
    notification_id, new_scheduled_at, confirm, force = parse_requeue_args(message)
    if notification_id is None or new_scheduled_at is None:
        await message.answer("Использование: /requeue &lt;notification_id&gt; &lt;YYYY-MM-DD&gt; &lt;HH:MM&gt; CONFIRM [FORCE]")
        return
    if new_scheduled_at <= datetime.now():
        await message.answer("Новое время должно быть в будущем.")
        return

    with SessionLocal() as db:
        admin = get_message_admin(db, message)
        if not admin:
            await message.answer("Нет доступа")
            return
        notif = get_notification(db, notification_id)
        if not notif:
            await message.answer("Уведомление не найдено.")
            return
        if not notif.event:
            await message.answer("Связанная ППР не найдена.")
            return
        if not confirm:
            await message.answer(format_requeue_preview(notif, new_scheduled_at, force=force))
            return
        try:
            requeued = requeue_notification(
                db,
                notification_id,
                new_scheduled_at,
                admin,
                force=force,
            )
        except (PermissionError, LookupError, ValueError) as exc:
            await message.answer(escape(str(exc)))
            return

    await message.answer(
        f"Уведомление {requeued.id} возвращено в planned.\n"
        f"ППР: {escape(requeued.event.title)}\n"
        f"Новое время: {requeued.scheduled_at.strftime('%Y-%m-%d %H:%M')}\n"
        "Автоотправка: включена"
    )


@dp.message(Command("reset_test_statuses"))
async def on_reset_test_statuses(message: Message):
    if not strict_dev_commands_allowed():
        await message.answer("Команда /reset_test_statuses доступна только при DEV_COMMANDS_ENABLED=true.")
        return

    with SessionLocal() as db:
        changed = reset_test_notification_statuses(db)

    await message.answer(f"Тестовые статусы сброшены: {changed}.")


@dp.message(Command("status"))
async def on_status(message: Message):
    with SessionLocal() as db:
        counts = get_status_counts(db)
    lines = [
        "Статус ППР",
        "",
        f"telegram enabled: {str(settings.telegram_enabled).lower()}",
        f"auto send: {'enabled' if settings.notifications_auto_send_enabled else 'disabled'}",
        f"auto send poll interval: {settings.auto_send_poll_interval_seconds}s",
        f"auto send max attempts: {settings.auto_send_max_attempts}",
        f"auto send retry delay: {settings.auto_send_retry_delay_seconds}s",
        f"auto send max late: {settings.auto_send_max_late_minutes}m",
        f"deployment mode: {settings.deployment_mode}",
        f"miniapp: {'enabled' if settings.miniapp_enabled else 'disabled'}",
        f"webapp url: {'configured' if settings.public_webapp_enabled else 'not configured'}",
        f"card deep link: {'configured' if is_card_deep_link_configured() else 'not configured'}",
        "",
        f"all ppr events: {counts['all_ppr_events']}",
        f"missing date: {counts['missing_date']}",
        f"total notifications: {counts['total_notifications']}",
        "",
        "notifications by status:",
    ]
    lines.extend(f"{status}: {count}" for status, count in counts["by_status"].items())
    await message.answer(
        "\n".join(lines)
    )


@dp.callback_query(F.data.startswith("details:"))
async def on_details(callback: CallbackQuery):
    try:
        notification_id = int(callback.data.split(":", 1)[1])
    except (AttributeError, ValueError):
        await callback.answer("Некорректный идентификатор", show_alert=True)
        return

    telegram_user = callback.from_user
    with SessionLocal() as db:
        app_user = get_or_sync_user(db, str(telegram_user.id), telegram_user.username, telegram_user_full_name(telegram_user))
        if not app_user or not app_user.is_active or app_user.role not in {ROLE_ADMIN, ROLE_CHECKER}:
            await callback.answer("Нет доступа", show_alert=True)
            return
        notif = get_notification(db, notification_id)
        if not notif:
            await callback.answer("ППР не найдена", show_alert=True)
            return
        details = render_notification_details(notif)
        keyboard = notification_keyboard(notif.id, notif.event.ppr_status)

    if not callback.message:
        await callback.answer("Исходное сообщение недоступно", show_alert=True)
        return

    try:
        new_message = await callback.message.answer(
            details,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception:
        logger.exception("Failed to create details message notification_id=%s", notification_id)
        await callback.answer("Не удалось открыть подробности", show_alert=True)
        return

    new_chat_id = getattr(getattr(new_message, "chat", None), "id", None)
    new_message_id = getattr(new_message, "message_id", None)
    if new_chat_id is None or new_message_id is None:
        logger.error("Details message notification_id=%s did not contain chat/message id.", notification_id)
        await callback.answer("Не удалось сохранить подробности", show_alert=True)
        return

    try:
        with SessionLocal() as db:
            fresh = get_notification(db, notification_id)
            if not fresh:
                logger.error("Notification %s disappeared before details message could be saved.", notification_id)
                await callback.answer("ППР не найдена", show_alert=True)
                return
            fresh.telegram_chat_id = str(new_chat_id)
            fresh.telegram_message_id = str(new_message_id)
            db.commit()
    except Exception:
        logger.exception("Failed to save details message ids notification_id=%s", notification_id)
        await callback.answer("Не удалось сохранить подробности", show_alert=True)
        return

    try:
        await callback.message.delete()
    except Exception:
        logger.warning("Failed to delete original details message notification_id=%s", notification_id, exc_info=True)

    await callback.answer()


@dp.callback_query(F.data.startswith("take:"))
async def on_take(callback: CallbackQuery, bot: Bot):
    notification_id = int(callback.data.split(":", 1)[1])
    user = callback.from_user
    with SessionLocal() as db:
        app_user = get_or_sync_user(db, str(user.id), user.username, telegram_user_full_name(user))
        if not app_user or not app_user.is_active or app_user.role not in {ROLE_ADMIN, ROLE_CHECKER}:
            await callback.answer("Нет доступа", show_alert=True)
            return

        ok, message, notif = take_notification(db, notification_id, app_user.telegram_id, user_display(app_user))
        if ok and notif:
            fresh = get_notification(db, notification_id)
            if fresh and callback.message:
                fresh.telegram_chat_id = fresh.telegram_chat_id or str(callback.message.chat.id)
                fresh.telegram_message_id = fresh.telegram_message_id or str(callback.message.message_id)
                db.commit()
                db.refresh(fresh)
            if fresh:
                await edit_saved_notification_message(bot, fresh)
    await callback.answer(message, show_alert=not ok)


@dp.callback_query(F.data.startswith("check:"))
async def on_check(callback: CallbackQuery, bot: Bot):
    notification_id = int(callback.data.split(":", 1)[1])
    user = callback.from_user
    with SessionLocal() as db:
        app_user = get_or_sync_user(db, str(user.id), user.username, telegram_user_full_name(user))
        if not app_user or not app_user.is_active or app_user.role not in {ROLE_ADMIN, ROLE_CHECKER}:
            await callback.answer("Нет доступа", show_alert=True)
            return

        ok, message, notif = check_notification(db, notification_id, app_user.telegram_id, user_display(app_user), is_admin=app_user.role == ROLE_ADMIN)
        if ok and notif:
            fresh = get_notification(db, notification_id)
            if fresh and callback.message:
                fresh.telegram_chat_id = fresh.telegram_chat_id or str(callback.message.chat.id)
                fresh.telegram_message_id = fresh.telegram_message_id or str(callback.message.message_id)
                db.commit()
                db.refresh(fresh)
            if fresh:
                await edit_saved_notification_message(bot, fresh)
    await callback.answer(message, show_alert=not ok)


async def main():
    if not settings.telegram_enabled:
        logger.warning("Telegram bot disabled: TELEGRAM_ENABLED=false.")
        return
    if not settings.telegram_bot_token:
        logger.error("Telegram bot cannot start: TELEGRAM_BOT_TOKEN is empty.")
        return
    if settings.miniapp_enabled and not is_card_deep_link_configured():
        logger.warning("TELEGRAM_BOT_USERNAME is empty, card deep link button disabled.")
    elif not settings.miniapp_enabled:
        logger.info("Deployment mode bot_only: Mini App card button is disabled.")

    bot = Bot(token=settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    auto_send_task: asyncio.Task | None = None
    if not settings.notifications_auto_send_enabled:
        logger.warning("Automatic notification sending disabled.")
    else:
        logger.warning("AUTO SEND ENABLED: due notifications will be sent automatically")
        if settings.auto_send_allow_mass:
            logger.warning("!!! AUTO_SEND_ALLOW_MASS=true: mass auto-send protection is disabled !!!")
    if settings.notifications_auto_send_enabled and telegram_sending_available(log_reason=True):
        auto_send_task = asyncio.create_task(auto_send_loop(bot))

    try:
        logger.info("Telegram bot polling started.")
        await dp.start_polling(bot)
    finally:
        if auto_send_task:
            auto_send_task.cancel()
            try:
                await auto_send_task
            except asyncio.CancelledError:
                pass
            with SessionLocal() as db:
                mark_scheduler_stopped(db, WORKER_ID)
        await bot.session.close()


if __name__ == "__main__":
    configure_logging()
    asyncio.run(main())
