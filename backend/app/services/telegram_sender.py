import logging
import socket
from datetime import date, datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter, TelegramUnauthorizedError
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload
from sqlalchemy import and_, desc, func, or_, update

from app.config import get_settings
from app.bot.keyboards import notification_keyboard
from app.db.models import AuditLog, PprEvent, PprNotification, SchedulerHeartbeat
from app.services.statuses import (
    NOTIFICATION_STATUS_DELIVERY_UNKNOWN,
    NOTIFICATION_STATUS_FAILED,
    NOTIFICATION_STATUS_PLANNED,
    NOTIFICATION_STATUS_PROCESSING,
    NOTIFICATION_STATUS_SENT,
    NOTIFICATION_STATUS_SKIPPED,
    NOTIFICATION_STATUS_CANCELLED,
    PPR_STATUS_ARCHIVED,
    PPR_STATUS_IN_PROGRESS,
    PPR_STATUS_SCHEDULED,
    PPR_STATUS_VERIFIED,
)

logger = logging.getLogger(__name__)


STATUS_MAP = {
    PPR_STATUS_SCHEDULED: "ожидает взятия в работу",
    PPR_STATUS_IN_PROGRESS: "в работе",
    PPR_STATUS_VERIFIED: "проверено",
    PPR_STATUS_ARCHIVED: "архивировано",
}

KNOWN_NOTIFICATION_STATUSES = [
    NOTIFICATION_STATUS_PLANNED,
    NOTIFICATION_STATUS_PROCESSING,
    NOTIFICATION_STATUS_DELIVERY_UNKNOWN,
    NOTIFICATION_STATUS_SENT,
    NOTIFICATION_STATUS_FAILED,
    NOTIFICATION_STATUS_SKIPPED,
    NOTIFICATION_STATUS_CANCELLED,
]

HISTORY_PPR_STATUSES = {PPR_STATUS_VERIFIED, PPR_STATUS_IN_PROGRESS}
HISTORY_STATUSES = set(KNOWN_NOTIFICATION_STATUSES) | HISTORY_PPR_STATUSES

PROCESSING_PHASE_CLAIMED = "claimed"
PROCESSING_PHASE_SENDING = "sending"
UNKNOWN_DELIVERY_ERROR = "Worker stopped while delivery result was unknown"


def telegram_sending_available(log_reason: bool = False) -> bool:
    settings = get_settings()
    if not settings.telegram_enabled:
        if log_reason:
            logger.warning("Telegram sending disabled.")
        return False
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        if log_reason:
            logger.error("Telegram sending enabled, but TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is empty. Notifications will not be sent.")
        return False
    return True


AUTOSEND_REASON = "planned, auto_send_enabled, active PPR, notify enabled, scheduled_at <= now, not too late"


def now_dt() -> datetime:
    return datetime.now()


def utc_now() -> datetime:
    return datetime.utcnow()


def late_cutoff() -> datetime:
    return now_dt() - timedelta(minutes=max(0, get_settings().auto_send_max_late_minutes))


def sanitize_error(exc: Exception | str) -> str:
    text = str(exc)
    settings = get_settings()
    for secret in [settings.telegram_bot_token, settings.outlook_client_secret]:
        if secret:
            text = text.replace(secret, "[secret]")
    return text[:1000]


def should_retry_error(exc: Exception) -> bool:
    if isinstance(exc, (TelegramUnauthorizedError, TelegramForbiddenError, TelegramBadRequest)):
        return False
    return isinstance(exc, (TelegramRetryAfter, TelegramNetworkError, TimeoutError, ConnectionError, OSError, socket.timeout))


def retry_delay_for_error(exc: Exception) -> int:
    if isinstance(exc, TelegramRetryAfter):
        return max(1, int(getattr(exc, "retry_after", 0) or 0))
    return max(1, get_settings().auto_send_retry_delay_seconds)


def processing_stale_cutoff() -> datetime:
    return utc_now() - timedelta(seconds=max(1, get_settings().processing_stale_after_seconds))


def mark_scheduler_started(db: Session, worker_id: str) -> SchedulerHeartbeat:
    now = utc_now()
    heartbeat = db.query(SchedulerHeartbeat).filter(SchedulerHeartbeat.worker_id == worker_id).one_or_none()
    if heartbeat is None:
        heartbeat = SchedulerHeartbeat(worker_id=worker_id, started_at=now)
        db.add(heartbeat)
    heartbeat.started_at = now
    heartbeat.is_running = True
    heartbeat.last_poll_error = None
    heartbeat.updated_at = now
    db.commit()
    db.refresh(heartbeat)
    return heartbeat


def mark_scheduler_poll_started(db: Session, worker_id: str) -> None:
    heartbeat = db.query(SchedulerHeartbeat).filter(SchedulerHeartbeat.worker_id == worker_id).one_or_none()
    if heartbeat is None:
        heartbeat = SchedulerHeartbeat(worker_id=worker_id, started_at=utc_now())
        db.add(heartbeat)
    now = utc_now()
    heartbeat.last_poll_at = now
    heartbeat.is_running = True
    heartbeat.updated_at = now
    db.commit()


def mark_scheduler_poll_finished(db: Session, worker_id: str, error: Exception | str | None = None) -> None:
    heartbeat = db.query(SchedulerHeartbeat).filter(SchedulerHeartbeat.worker_id == worker_id).one_or_none()
    if heartbeat is None:
        heartbeat = SchedulerHeartbeat(worker_id=worker_id, started_at=utc_now())
        db.add(heartbeat)
    now = utc_now()
    if error is None:
        heartbeat.last_successful_poll_at = now
        heartbeat.last_poll_error = None
    else:
        heartbeat.last_poll_error = sanitize_error(error)
    heartbeat.is_running = True
    heartbeat.updated_at = now
    db.commit()


def mark_scheduler_stopped(db: Session, worker_id: str) -> None:
    heartbeat = db.query(SchedulerHeartbeat).filter(SchedulerHeartbeat.worker_id == worker_id).one_or_none()
    if heartbeat:
        heartbeat.is_running = False
        heartbeat.updated_at = utc_now()
        db.commit()


def get_latest_scheduler_heartbeat(db: Session) -> SchedulerHeartbeat | None:
    return db.query(SchedulerHeartbeat).order_by(SchedulerHeartbeat.last_poll_at.desc().nullslast(), SchedulerHeartbeat.updated_at.desc()).first()


def due_notifications_query(db: Session, *, include_too_late: bool = False):
    now = datetime.now()
    retry_cutoff = now - timedelta(seconds=max(1, get_settings().auto_send_retry_delay_seconds))
    return (
        db.query(PprNotification)
        .options(joinedload(PprNotification.event))
        .join(PprNotification.event)
        .filter(PprNotification.status == NOTIFICATION_STATUS_PLANNED)
        .filter(PprNotification.auto_send_enabled.is_(True))
        .filter(PprEvent.is_active.is_(True))
        .filter(PprEvent.ppr_status == PPR_STATUS_SCHEDULED)
        .filter(PprEvent.date.isnot(None))
        .filter(PprNotification.scheduled_at <= now)
        .filter(or_(PprNotification.last_attempt_at.is_(None), PprNotification.last_attempt_at <= retry_cutoff))
        .filter(PprNotification.attempt_count < max(1, get_settings().auto_send_max_attempts))
        .filter(or_(include_too_late, PprNotification.scheduled_at >= late_cutoff()))
        .filter(
            or_(
                and_(
                    PprNotification.type == "start",
                    PprEvent.notify_start.is_(True),
                    PprEvent.start_time.isnot(None),
                ),
                and_(
                    PprNotification.type == "end",
                    PprEvent.notify_end.is_(True),
                    PprEvent.end_time.isnot(None),
                ),
            )
        )
        .order_by(PprNotification.scheduled_at.asc())
    )


def too_late_notifications_query(db: Session):
    return due_notifications_query(db, include_too_late=True).filter(PprNotification.scheduled_at < late_cutoff())


def count_due_notifications(db: Session) -> int:
    return due_notifications_query(db).count()


def get_due_notifications(db: Session, limit: int | None = 20) -> list[PprNotification]:
    query = due_notifications_query(db)
    if limit is not None:
        query = query.limit(limit)
    return query.all()


def get_autosend_blocked_reason(total_due: int, respect_global_enabled: bool = True) -> str | None:
    settings = get_settings()
    if total_due <= 0:
        return None
    if respect_global_enabled and not settings.notifications_auto_send_enabled:
        return "NOTIFICATIONS_AUTO_SEND_ENABLED=false"
    if total_due > settings.auto_send_mass_limit and not settings.auto_send_allow_mass:
        return (
            f"due notifications ({total_due}) exceed AUTO_SEND_MASS_LIMIT="
            f"{settings.auto_send_mass_limit}; AUTO_SEND_ALLOW_MASS=false"
        )
    return None


def build_autosend_preview(db: Session, limit: int = 10, respect_global_enabled: bool = False) -> dict:
    settings = get_settings()
    safe_limit = max(1, min(limit, 100))
    total_due = count_due_notifications(db)
    total_too_late = too_late_notifications_query(db).count()
    mass_send_blocked = total_due > settings.auto_send_mass_limit and not settings.auto_send_allow_mass
    blocked_reason = get_autosend_blocked_reason(total_due, respect_global_enabled=respect_global_enabled)
    notifications = get_due_notifications(db, limit=safe_limit)
    return {
        "summary": {
            "total_due": total_due,
            "mass_send_limit": settings.auto_send_mass_limit,
            "global_auto_send_enabled": settings.notifications_auto_send_enabled,
            "mass_send_allowed": settings.auto_send_allow_mass,
            "mass_send_blocked": mass_send_blocked,
            "would_send": 0 if blocked_reason else total_due,
            "blocked_reason": blocked_reason,
            "too_late": total_too_late,
        },
        "items": [
            {
                "notification_id": notif.id,
                "ppr_event_id": notif.ppr_event_id,
                "title": notif.event.title,
                "project": notif.event.project,
                "planned_datetime": notif.scheduled_at.isoformat(),
                "status": notif.status,
                "auto_send_enabled": notif.auto_send_enabled,
                "reason": AUTOSEND_REASON,
            }
            for notif in notifications
        ],
    }


def get_next_planned_notification(db: Session) -> PprNotification | None:
    return (
        db.query(PprNotification)
        .options(joinedload(PprNotification.event))
        .join(PprNotification.event)
        .filter(PprNotification.status == NOTIFICATION_STATUS_PLANNED)
        .filter(PprEvent.is_active.is_(True))
        .order_by(PprNotification.scheduled_at.asc())
        .first()
    )


def claim_notification_for_processing(db: Session, notification_id: int, worker_id: str) -> PprNotification | None:
    now = utc_now()
    result = db.execute(
        update(PprNotification)
        .where(PprNotification.id == notification_id)
        .where(PprNotification.status == NOTIFICATION_STATUS_PLANNED)
        .values(
            status=NOTIFICATION_STATUS_PROCESSING,
            processing_started_at=now,
            processing_by=worker_id,
            processing_phase=PROCESSING_PHASE_CLAIMED,
            attempt_count=PprNotification.attempt_count + 1,
            last_attempt_at=now,
            updated_at=now,
        )
    )
    db.commit()
    if result.rowcount != 1:
        return None
    return get_notification_for_sending(db, notification_id)


def complete_notification_sent(db: Session, notif: PprNotification, chat_id: str | int, message_id: str | int) -> None:
    now = utc_now()
    notif.telegram_chat_id = str(chat_id)
    notif.telegram_message_id = str(message_id)
    notif.sent_at = now
    notif.status = NOTIFICATION_STATUS_SENT
    notif.processing_started_at = None
    notif.processing_by = None
    notif.processing_phase = None
    notif.last_error = None
    notif.updated_at = now
    db.commit()


def complete_notification_error(db: Session, notif: PprNotification, exc: Exception) -> str:
    settings = get_settings()
    now = utc_now()
    error_text = sanitize_error(exc)
    retryable = should_retry_error(exc)
    max_attempts_reached = (notif.attempt_count or 0) >= max(1, settings.auto_send_max_attempts)
    notif.last_error = error_text
    notif.processing_started_at = None
    notif.processing_by = None
    notif.processing_phase = None
    notif.updated_at = now
    if retryable and not max_attempts_reached:
        notif.status = NOTIFICATION_STATUS_PLANNED
        delay = retry_delay_for_error(exc)
        notif.last_attempt_at = now + timedelta(seconds=max(0, delay - settings.auto_send_retry_delay_seconds))
    else:
        notif.status = NOTIFICATION_STATUS_FAILED
    db.commit()
    return error_text


def skip_too_late_notifications(db: Session) -> int:
    notifications = too_late_notifications_query(db).all()
    now = utc_now()
    for notif in notifications:
        result = db.execute(
            update(PprNotification)
            .where(PprNotification.id == notif.id)
            .where(PprNotification.status == NOTIFICATION_STATUS_PLANNED)
            .values(
                status=NOTIFICATION_STATUS_SKIPPED,
                last_error="too_late",
                processing_started_at=None,
                processing_by=None,
                processing_phase=None,
                updated_at=now,
            )
        )
        if result.rowcount:
            logger.warning(
                "AUTO SEND SKIPPED notification_id=%s ppr_event_id=%s reason=too_late scheduled_at=%s",
                notif.id,
                notif.ppr_event_id,
                notif.scheduled_at,
            )
    db.commit()
    return len(notifications)


def recover_stale_processing_notifications(db: Session) -> dict[str, int]:
    stale = (
        db.query(PprNotification)
        .filter(PprNotification.status == NOTIFICATION_STATUS_PROCESSING)
        .filter(PprNotification.processing_started_at.isnot(None))
        .filter(PprNotification.processing_started_at < processing_stale_cutoff())
        .all()
    )
    now = utc_now()
    recovered_claimed = 0
    unknown = 0
    for notif in stale:
        if notif.processing_phase == PROCESSING_PHASE_CLAIMED:
            notif.status = NOTIFICATION_STATUS_PLANNED
            notif.processing_started_at = None
            notif.processing_by = None
            notif.processing_phase = None
            notif.last_error = "Recovered stale claimed notification before Telegram request"
            recovered_claimed += 1
            db.add(AuditLog(notification_id=notif.id, ppr_event_id=notif.ppr_event_id, action="stale_processing_recovered", user_id="system", user_name="scheduler", comment=notif.last_error))
            logger.warning("Recovered stale claimed notification_id=%s ppr_event_id=%s to planned.", notif.id, notif.ppr_event_id)
        else:
            previous_worker = notif.processing_by
            notif.status = NOTIFICATION_STATUS_DELIVERY_UNKNOWN
            notif.processing_by = None
            notif.processing_phase = None
            notif.last_error = UNKNOWN_DELIVERY_ERROR
            unknown += 1
            db.add(AuditLog(notification_id=notif.id, ppr_event_id=notif.ppr_event_id, action="delivery_unknown", user_id="system", user_name="scheduler", comment=f"{UNKNOWN_DELIVERY_ERROR}; previous_worker={previous_worker or 'unknown'}"))
            logger.warning("Marked stale processing notification_id=%s ppr_event_id=%s as delivery_unknown.", notif.id, notif.ppr_event_id)
        notif.updated_at = now
    if stale:
        db.commit()
    return {"recovered_claimed": recovered_claimed, "delivery_unknown": unknown}


def get_planned_notifications(db: Session, limit: int = 10) -> list[PprNotification]:
    return (
        db.query(PprNotification)
        .options(joinedload(PprNotification.event))
        .join(PprNotification.event)
        .filter(PprNotification.status == NOTIFICATION_STATUS_PLANNED)
        .filter(PprEvent.is_active.is_(True))
        .order_by(PprNotification.scheduled_at.asc())
        .limit(limit)
        .all()
    )


def get_notification_for_sending(db: Session, notification_id: int) -> PprNotification | None:
    return (
        db.query(PprNotification)
        .options(joinedload(PprNotification.event))
        .filter(PprNotification.id == notification_id)
        .one_or_none()
    )


def list_notifications(
    db: Session,
    *,
    status: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    ppr_event_id: int | None = None,
    failed_only: bool = False,
    page: int = 1,
    page_size: int = 25,
) -> dict:
    safe_page = max(1, page)
    safe_page_size = max(1, min(page_size, 100))
    query = db.query(PprNotification).options(joinedload(PprNotification.event)).join(PprNotification.event)
    if failed_only:
        query = query.filter(PprNotification.status == NOTIFICATION_STATUS_FAILED)
    elif status:
        query = query.filter(PprNotification.status == status)
    if date_from:
        query = query.filter(PprNotification.scheduled_at >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        query = query.filter(PprNotification.scheduled_at <= datetime.combine(date_to, datetime.max.time()))
    if ppr_event_id:
        query = query.filter(PprNotification.ppr_event_id == ppr_event_id)
    total = query.count()
    items = (
        query.order_by(PprNotification.scheduled_at.asc(), PprNotification.id.asc())
        .offset((safe_page - 1) * safe_page_size)
        .limit(safe_page_size)
        .all()
    )
    return {"page": safe_page, "page_size": safe_page_size, "total": total, "items": items}


def list_notification_history(db: Session, *, status: str | None = None, limit: int = 10) -> list[PprNotification]:
    if status is not None and status not in HISTORY_STATUSES:
        raise ValueError(f"Неподдерживаемый статус: {status}")
    safe_limit = max(1, min(limit, 50))
    query = db.query(PprNotification).options(joinedload(PprNotification.event)).join(PprNotification.event)
    if status in HISTORY_PPR_STATUSES:
        query = query.filter(PprEvent.ppr_status == status)
    elif status:
        query = query.filter(PprNotification.status == status)
    return (
        query.order_by(desc(PprNotification.scheduled_at), desc(PprNotification.id))
        .limit(safe_limit)
        .all()
    )


def retry_failed_notification(db: Session, notification_id: int) -> PprNotification | None:
    notif = get_notification_for_sending(db, notification_id)
    if not notif:
        return None
    if notif.status != NOTIFICATION_STATUS_FAILED:
        return notif
    now = utc_now()
    notif.status = NOTIFICATION_STATUS_PLANNED
    notif.processing_started_at = None
    notif.processing_by = None
    notif.processing_phase = None
    notif.last_attempt_at = None
    notif.updated_at = now
    db.commit()
    db.refresh(notif)
    return notif


def mark_unknown_notification_sent(db: Session, notification_id: int, user, comment: str) -> PprNotification | None:
    notif = get_notification_for_sending(db, notification_id)
    if not notif:
        return None
    if notif.status == NOTIFICATION_STATUS_SENT:
        return notif
    if notif.status != NOTIFICATION_STATUS_DELIVERY_UNKNOWN:
        return notif
    now = utc_now()
    notif.status = NOTIFICATION_STATUS_SENT
    notif.sent_at = now
    notif.processing_started_at = None
    notif.processing_by = None
    notif.processing_phase = None
    notif.last_error = None
    notif.updated_at = now
    db.add(AuditLog(notification_id=notif.id, ppr_event_id=notif.ppr_event_id, action="delivery_unknown_mark_sent", user_id=user.telegram_id, user_name=getattr(user, "full_name", None) or getattr(user, "username", None), comment=comment))
    db.commit()
    db.refresh(notif)
    logger.warning("Admin %s marked delivery_unknown notification_id=%s as sent.", user.telegram_id, notif.id)
    return notif


def retry_unknown_notification(db: Session, notification_id: int, user, confirm: bool) -> PprNotification | None:
    notif = get_notification_for_sending(db, notification_id)
    if not notif:
        return None
    if notif.status != NOTIFICATION_STATUS_DELIVERY_UNKNOWN:
        return notif
    if not confirm:
        raise ValueError("confirm=true is required because Telegram may already have accepted the message")
    now = utc_now()
    notif.status = NOTIFICATION_STATUS_PLANNED
    notif.processing_started_at = None
    notif.processing_by = None
    notif.processing_phase = None
    notif.last_attempt_at = None
    notif.last_error = "Manually returned from delivery_unknown; duplicate delivery is possible"
    notif.updated_at = now
    db.add(AuditLog(notification_id=notif.id, ppr_event_id=notif.ppr_event_id, action="delivery_unknown_retry", user_id=user.telegram_id, user_name=getattr(user, "full_name", None) or getattr(user, "username", None), comment=notif.last_error))
    db.commit()
    db.refresh(notif)
    logger.warning("Admin %s returned delivery_unknown notification_id=%s to planned.", user.telegram_id, notif.id)
    return notif


def scheduler_status(db: Session) -> dict:
    settings = get_settings()
    heartbeat = get_latest_scheduler_heartbeat(db)
    now = utc_now()
    last_poll_at = heartbeat.last_poll_at if heartbeat else None
    threshold = max(settings.auto_send_poll_interval_seconds * 3, settings.processing_stale_after_seconds)
    scheduler_running = bool(heartbeat and heartbeat.is_running and last_poll_at and (now - last_poll_at).total_seconds() <= threshold)
    return {
        "auto_send_enabled": settings.notifications_auto_send_enabled,
        "scheduler_running": scheduler_running,
        "worker_id": heartbeat.worker_id if heartbeat else None,
        "started_at": heartbeat.started_at.isoformat() if heartbeat and heartbeat.started_at else None,
        "last_poll_at": last_poll_at.isoformat() if last_poll_at else None,
        "last_successful_poll_at": heartbeat.last_successful_poll_at.isoformat() if heartbeat and heartbeat.last_successful_poll_at else None,
        "last_poll_error": heartbeat.last_poll_error if heartbeat else None,
        "due_count": count_due_notifications(db),
        "processing_count": db.query(PprNotification).filter(PprNotification.status == NOTIFICATION_STATUS_PROCESSING).count(),
        "stale_processing_count": db.query(PprNotification).filter(PprNotification.status == NOTIFICATION_STATUS_PROCESSING, PprNotification.processing_started_at.isnot(None), PprNotification.processing_started_at < processing_stale_cutoff()).count(),
        "failed_count": db.query(PprNotification).filter(PprNotification.status == NOTIFICATION_STATUS_FAILED).count(),
        "delivery_unknown_count": db.query(PprNotification).filter(PprNotification.status == NOTIFICATION_STATUS_DELIVERY_UNKNOWN).count(),
    }


def normalize_status_key(status: str | None) -> str:
    if status is None:
        return "null"
    normalized = status.strip()
    return normalized or "empty"


def get_status_counts(db: Session) -> dict:
    by_status = {status: 0 for status in KNOWN_NOTIFICATION_STATUSES}
    rows = db.query(PprNotification.status, func.count(PprNotification.id)).group_by(PprNotification.status).all()
    for status, count in rows:
        by_status[normalize_status_key(status)] = int(count)

    counts = {
        "all_ppr_events": db.query(PprEvent).count(),
        "missing_date": db.query(PprEvent).filter(PprEvent.date.is_(None)).count(),
        "total_notifications": db.query(PprNotification).count(),
        "by_status": by_status,
    }
    counts.update({status: by_status.get(status, 0) for status in KNOWN_NOTIFICATION_STATUSES})
    return counts


def reset_test_notification_statuses(db: Session) -> int:
    notifications = (
        db.query(PprNotification)
        .filter(PprNotification.status.in_([NOTIFICATION_STATUS_SENT, NOTIFICATION_STATUS_FAILED, NOTIFICATION_STATUS_SKIPPED, NOTIFICATION_STATUS_PROCESSING, NOTIFICATION_STATUS_DELIVERY_UNKNOWN]))
        .all()
    )
    now = datetime.utcnow()
    for notif in notifications:
        notif.status = NOTIFICATION_STATUS_PLANNED
        notif.telegram_chat_id = None
        notif.telegram_message_id = None
        notif.sent_at = None
        notif.processing_started_at = None
        notif.processing_by = None
        notif.processing_phase = None
        notif.processing_phase = None
        notif.last_error = None
        notif.taken_by_id = None
        notif.taken_by_name = None
        notif.taken_at = None
        notif.checked_by_id = None
        notif.checked_by_name = None
        notif.checked_at = None
        notif.updated_at = now
    db.commit()
    return len(notifications)


def get_today_notifications(db: Session) -> list[PprNotification]:
    today = date.today()
    return (
        db.query(PprNotification)
        .options(joinedload(PprNotification.event))
        .join(PprNotification.event)
        .filter(PprEvent.date == today)
        .filter(PprEvent.is_active.is_(True))
        .order_by(PprNotification.scheduled_at.asc())
        .all()
    )


def render_notification_brief(notif: PprNotification) -> str:
    e = notif.event
    time_text = e.start_time.strftime("%H:%M") if e.start_time else "--:--"
    project = f" — {escape(e.project)}" if e.project else ""
    return f"#{notif.id} {time_text} {escape(e.title)}{project} [{STATUS_MAP.get(e.ppr_status, e.ppr_status)}]"


def format_telegram_datetime(value: datetime | None) -> str | None:
    """Format UTC status timestamps in the configured project timezone."""
    if value is None:
        return None
    utc_value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return utc_value.astimezone(ZoneInfo(get_settings().default_timezone)).strftime("%d.%m.%Y %H:%M")


def render_notification_message(notif: PprNotification) -> str:
    e = notif.event
    kind = "ППР должна выйти" if notif.type == "start" else "ППР должна завершиться"
    lines = [
        f"🔔 <b>{kind}</b>",
        "",
        f"<b>Название:</b> {escape(e.title)}",
    ]
    if e.project:
        lines.append(f"<b>Проект:</b> {escape(e.project)}")
    if e.date:
        lines.append(f"<b>Дата:</b> {e.date.strftime('%d.%m.%Y')}")
    if e.start_time:
        lines.append(f"<b>Время выхода:</b> {e.start_time.strftime('%H:%M')}")
    lines.append(f"<b>Статус:</b> {STATUS_MAP.get(e.ppr_status, e.ppr_status)}")
    if e.activities:
        lines.append("")
        lines.append("<b>Активности:</b>")
        for idx, item in enumerate([x.strip() for x in e.activities.split(';') if x.strip()], start=1):
            lines.append(f"{idx}. {escape(item)}")
    if e.responsible_setup:
        lines.append(f"<b>Ответственный настройка:</b> {escape(e.responsible_setup)}")
    if e.responsible_report:
        lines.append(f"<b>Ответственный отчетка:</b> {escape(e.responsible_report)}")
    outlook_url = e.outlook_url or e.outlook_link
    if outlook_url:
        lines.append("")
        lines.append(f"📅 Outlook: <a href=\"{escape(outlook_url, quote=True)}\">открыть событие</a>")
    if e.source_link and not outlook_url:
        lines.append("")
        lines.append(f"🔗 <a href=\"{escape(e.source_link, quote=True)}\">Ссылка</a>")

    if notif.taken_by_name:
        lines.append(f"<b>Проверяющий:</b> {escape(notif.taken_by_name)}")
    taken_at = format_telegram_datetime(notif.taken_at)
    if taken_at:
        lines.append(f"<b>Взято:</b> {taken_at}")
    if notif.checked_by_name:
        lines.append(f"<b>Проверил:</b> {escape(notif.checked_by_name)}")
    checked_at = format_telegram_datetime(notif.checked_at)
    if checked_at:
        lines.append(f"<b>Проверено:</b> {checked_at}")
    return "\n".join(lines)


def render_notification_details(notif: PprNotification) -> str:
    """Render the complete PPR card for Telegram-only operation."""
    event = notif.event
    lines = ["<b>ППР: подробности</b>", "", f"<b>Название:</b> {escape(event.title)}"]
    if event.project:
        lines.append(f"<b>Проект:</b> {escape(event.project)}")
    lines.append(f"<b>Дата:</b> {event.date.strftime('%d.%m.%Y') if event.date else 'нет даты'}")
    lines.append(f"<b>Время выхода:</b> {event.start_time.strftime('%H:%M') if event.start_time else 'не указано'}")
    if event.end_time:
        lines.append(f"<b>Время завершения:</b> {event.end_time.strftime('%H:%M')}")
    lines.append(f"<b>Статус:</b> {STATUS_MAP.get(event.ppr_status, event.ppr_status)}")
    if event.activities:
        lines.extend(["", "<b>Активности:</b>"])
        lines.extend(f"{index}. {escape(item)}" for index, item in enumerate((part.strip() for part in event.activities.split(";") if part.strip()), start=1))
    if event.responsible_setup:
        lines.append(f"<b>Ответственный настройка:</b> {escape(event.responsible_setup)}")
    if event.responsible_report:
        lines.append(f"<b>Ответственный отчетка:</b> {escape(event.responsible_report)}")
    if event.comment:
        lines.append(f"<b>Комментарий:</b> {escape(event.comment)}")
    if notif.taken_by_name:
        lines.append(f"<b>Взял в работу:</b> {escape(notif.taken_by_name)}")
    taken_at = format_telegram_datetime(notif.taken_at)
    if taken_at:
        lines.append(f"<b>Взято:</b> {taken_at}")
    if notif.checked_by_name:
        lines.append(f"<b>Проверил:</b> {escape(notif.checked_by_name)}")
    checked_at = format_telegram_datetime(notif.checked_at)
    if checked_at:
        lines.append(f"<b>Проверено:</b> {checked_at}")
    outlook_url = event.outlook_url or event.outlook_link
    if outlook_url:
        lines.append(f"📅 Outlook: <a href=\"{escape(outlook_url, quote=True)}\">открыть событие</a>")

    text = "\n".join(lines)
    return text if len(text) <= 3900 else f"{text[:3897]}..."


async def send_notification(
    db: Session,
    notif: PprNotification,
    bot: Bot | None = None,
    chat_id: str | int | None = None,
    *,
    manual: bool = True,
    worker_id: str = "manual",
) -> bool:
    settings = get_settings()
    if not notif.event.is_active:
        return False
    if not notif.auto_send_enabled:
        logger.warning("Manual send requested for notification %s with auto_send_enabled=false.", notif.id)
    if chat_id is None and not telegram_sending_available():
        return False
    if chat_id is not None and (not settings.telegram_enabled or not settings.telegram_bot_token):
        return False

    own_bot = bot is None
    active_bot = bot or Bot(token=settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    claimed = notif
    try:
        if notif.status == NOTIFICATION_STATUS_PLANNED:
            claimed = claim_notification_for_processing(db, notif.id, worker_id)
            if not claimed:
                logger.info("Notification %s was already claimed by another worker.", notif.id)
                return False
        elif notif.status != NOTIFICATION_STATUS_PROCESSING:
            logger.warning("Notification %s has status %s and will not be sent.", notif.id, notif.status)
            return False
        claimed.processing_phase = PROCESSING_PHASE_SENDING
        claimed.updated_at = utc_now()
        db.commit()
        message = await active_bot.send_message(
            chat_id=chat_id or settings.telegram_chat_id,
            text=render_notification_message(claimed),
            reply_markup=notification_keyboard(claimed.id, claimed.event.ppr_status),
            disable_web_page_preview=True,
        )
        complete_notification_sent(db, claimed, chat_id or settings.telegram_chat_id, message.message_id)
        logger.info(
            "Notification sent notification_id=%s ppr_event_id=%s attempt=%s telegram_message_id=%s",
            claimed.id,
            claimed.ppr_event_id,
            claimed.attempt_count,
            message.message_id,
        )
        return True
    except Exception as exc:
        db.rollback()
        try:
            fresh = get_notification_for_sending(db, claimed.id)
            if fresh and fresh.status == NOTIFICATION_STATUS_PROCESSING:
                error_text = complete_notification_error(db, fresh, exc)
                logger.warning(
                    "Notification send failed notification_id=%s ppr_event_id=%s attempt=%s status=%s error=%s",
                    fresh.id,
                    fresh.ppr_event_id,
                    fresh.attempt_count,
                    fresh.status,
                    error_text,
                )
        except Exception:
            logger.exception("Failed to persist Telegram send error for notification %s.", getattr(claimed, "id", None))
        raise
    finally:
        if own_bot:
            await active_bot.session.close()


async def send_due_notifications(db: Session, bot: Bot | None = None, limit: int | None = None, worker_id: str | None = None) -> int:
    settings = get_settings()
    if not settings.notifications_auto_send_enabled:
        logger.warning("AUTO SEND SKIPPED: NOTIFICATIONS_AUTO_SEND_ENABLED=false.")
        return 0
    if not telegram_sending_available():
        return 0

    recovered = recover_stale_processing_notifications(db)
    if recovered["recovered_claimed"] or recovered["delivery_unknown"]:
        logger.warning("AUTO SEND recovered stale processing: %s.", recovered)

    skipped = skip_too_late_notifications(db)
    if skipped:
        logger.warning("AUTO SEND skipped %s too-late notification(s).", skipped)

    total_due = count_due_notifications(db)
    blocked_reason = get_autosend_blocked_reason(total_due, respect_global_enabled=True)
    if blocked_reason:
        logger.warning("AUTO SEND BLOCKED: %s. No notifications were sent.", blocked_reason)
        return 0

    sent = 0
    worker_id = worker_id or f"autosend:{id(bot) if bot else 'own'}"
    for notif in get_due_notifications(db, limit=limit or total_due):
        try:
            if await send_notification(db, notif, bot=bot, manual=False, worker_id=worker_id):
                sent += 1
        except Exception as exc:
            logger.exception("Failed to send notification %s: %s", notif.id, exc)
    return sent
