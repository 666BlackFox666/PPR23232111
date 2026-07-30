import hashlib
import re
from datetime import date, datetime, time

from sqlalchemy import text, update
from sqlalchemy.orm import Session, joinedload
from app.db.models import AuditLog, PprEvent, PprNotification
from app.services.statuses import (
    NOTIFICATION_STATUS_CANCELLED,
    NOTIFICATION_STATUS_PLANNED,
    NOTIFICATION_STATUS_SKIPPED,
    PPR_STATUS_ARCHIVED,
    PPR_STATUS_CANCELLED,
    PPR_STATUS_IN_PROGRESS,
    PPR_STATUS_SCHEDULED,
    PPR_STATUS_VERIFIED,
)
from app.services.user_service import ROLE_ADMIN, user_display


class WorkflowConflict(ValueError):
    pass


class PprDuplicateError(ValueError):
    def __init__(self, event_id: int):
        self.event_id = event_id
        super().__init__(f"Найдена потенциально дублирующая ППР: ID {event_id}")


def normalize_duplicate_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def find_active_ppr_duplicate(
    db: Session,
    *,
    title: str,
    project: str,
    event_date: date,
    start_time: time,
) -> PprEvent | None:
    normalized_title = normalize_duplicate_text(title)
    normalized_project = normalize_duplicate_text(project)
    candidates = (
        db.query(PprEvent)
        .filter(
            PprEvent.is_active.is_(True),
            PprEvent.date == event_date,
            PprEvent.start_time == start_time,
        )
        .order_by(PprEvent.id.asc())
        .all()
    )
    for event in candidates:
        if (
            normalize_duplicate_text(event.title) == normalized_title
            and normalize_duplicate_text(event.project) == normalized_project
        ):
            return event
    return None


def _create_duplicate_lock_key(
    *,
    title: str,
    project: str,
    event_date: date,
    start_time: time,
) -> int:
    identity = "\x1f".join(
        [
            normalize_duplicate_text(title),
            normalize_duplicate_text(project),
            event_date.isoformat(),
            start_time.isoformat(),
        ]
    )
    return int.from_bytes(
        hashlib.sha256(identity.encode("utf-8")).digest()[:8],
        byteorder="big",
        signed=True,
    )


def _acquire_create_duplicate_lock(
    db: Session,
    *,
    title: str,
    project: str,
    event_date: date,
    start_time: time,
) -> None:
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return
    lock_key = _create_duplicate_lock_key(
        title=title,
        project=project,
        event_date=event_date,
        start_time=start_time,
    )
    db.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})


def user_display_name(user_id: str, username: str | None, first_name: str | None = None, last_name: str | None = None) -> str:
    if username:
        return f"@{username}"
    name = " ".join([x for x in [first_name, last_name] if x]).strip()
    return name or str(user_id)


def get_notification(db: Session, notification_id: int) -> PprNotification | None:
    return (
        db.query(PprNotification)
        .options(
            joinedload(PprNotification.event).joinedload(PprEvent.audit_logs),
            joinedload(PprNotification.audit_logs),
        )
        .filter(PprNotification.id == notification_id)
        .one_or_none()
    )


def get_event_card(db: Session, ppr_id: int) -> PprEvent | None:
    return (
        db.query(PprEvent)
        .options(joinedload(PprEvent.notifications).joinedload(PprNotification.audit_logs), joinedload(PprEvent.audit_logs))
        .filter(PprEvent.id == ppr_id)
        .one_or_none()
    )


def parse_date_input(value: str | date | None) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def parse_time_input(value: str | time | None) -> time | None:
    if value in (None, ""):
        return None
    if isinstance(value, time):
        return value.replace(microsecond=0)
    text = str(value)
    if len(text) == 5:
        text = f"{text}:00"
    return time.fromisoformat(text).replace(microsecond=0)


def combine_datetime(d: date | None, t: time | None) -> datetime | None:
    if d is None or t is None:
        return None
    return datetime.combine(d, t)


def add_event_audit(db: Session, event: PprEvent, action: str, user, comment: str | None = None) -> None:
    db.add(
        AuditLog(
            ppr_event_id=event.id,
            action=action,
            user_id=user.telegram_id,
            user_name=user_display(user),
            comment=comment,
        )
    )


def add_notification_audit(db: Session, notification: PprNotification, action: str, user, comment: str | None = None) -> None:
    db.add(
        AuditLog(
            notification_id=notification.id,
            ppr_event_id=notification.ppr_event_id,
            action=action,
            user_id=user.telegram_id,
            user_name=user_display(user),
            comment=comment,
        )
    )


def format_value(value) -> str:
    if value is None:
        return "пусто"
    if isinstance(value, (date, time, datetime)):
        return value.isoformat()
    if isinstance(value, bool):
        return "да" if value else "нет"
    return str(value)


def describe_changes(changes: dict[str, tuple[object, object]]) -> str:
    return "; ".join(f"{field}: {format_value(old)} -> {format_value(new)}" for field, (old, new) in changes.items())


def reset_notification_workflow(notif: PprNotification) -> None:
    notif.telegram_chat_id = None
    notif.telegram_message_id = None
    notif.sent_at = None
    notif.processing_started_at = None
    notif.processing_by = None
    notif.processing_phase = None
    notif.last_error = None
    notif.telegram_edit_last_error = None
    notif.taken_by_id = None
    notif.taken_by_name = None
    notif.taken_at = None
    notif.checked_by_id = None
    notif.checked_by_name = None
    notif.checked_at = None
    notif.updated_at = datetime.utcnow()


def infer_restored_status(event: PprEvent) -> str:
    notification = sorted(event.notifications, key=lambda n: (n.scheduled_at, n.id))[0] if event.notifications else None
    if notification and notification.checked_by_id:
        return PPR_STATUS_VERIFIED
    if notification and notification.taken_by_id:
        return PPR_STATUS_IN_PROGRESS
    return PPR_STATUS_SCHEDULED


def sync_start_notification(db: Session, event: PprEvent, user, audit: bool = True) -> PprNotification | None:
    notif = (
        db.query(PprNotification)
        .filter(PprNotification.ppr_event_id == event.id, PprNotification.type == "start")
        .one_or_none()
    )
    scheduled_at = combine_datetime(event.date, event.start_time)
    enabled = bool(event.is_active and event.notify_start and scheduled_at)

    if not enabled:
        if notif:
            notif.status = NOTIFICATION_STATUS_CANCELLED if not event.is_active else NOTIFICATION_STATUS_SKIPPED
            notif.auto_send_enabled = False
            notif.updated_at = datetime.utcnow()
            if audit:
                add_notification_audit(db, notif, "notification_disabled", user, "Уведомление отключено")
        return notif

    if notif is None:
        notif = PprNotification(ppr_event_id=event.id, type="start", scheduled_at=scheduled_at, status=NOTIFICATION_STATUS_PLANNED)
        db.add(notif)
        db.flush()
        if audit:
            add_notification_audit(db, notif, "notification_enabled", user, "Уведомление создано")
    else:
        notif.scheduled_at = scheduled_at
        if event.ppr_status == PPR_STATUS_SCHEDULED:
            notif.status = NOTIFICATION_STATUS_PLANNED
            reset_notification_workflow(notif)
        elif notif.status in {NOTIFICATION_STATUS_SKIPPED, NOTIFICATION_STATUS_CANCELLED}:
            notif.status = NOTIFICATION_STATUS_PLANNED
        if audit:
            add_notification_audit(db, notif, "notification_updated", user, f"scheduled_at: {scheduled_at.isoformat()}")

    notif.auto_send_enabled = scheduled_at > datetime.now()
    notif.updated_at = datetime.utcnow()
    return notif


def make_manual_external_id() -> str:
    return f"MANUAL-{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}"


def apply_event_payload(event: PprEvent, payload: dict) -> dict[str, tuple[object, object]]:
    changes = {}
    field_map = {
        "title": "title",
        "project": "project",
        "activities": "activities",
        "outlook_link": "outlook_link",
        "comment": "comment",
    }
    for key, attr in field_map.items():
        if key in payload:
            new_value = payload[key]
            old_value = getattr(event, attr)
            if old_value != new_value:
                setattr(event, attr, new_value)
                changes[attr] = (old_value, new_value)

    if "date" in payload:
        new_date = parse_date_input(payload["date"])
        if event.date != new_date:
            changes["date"] = (event.date, new_date)
            event.date = new_date

    if "start_time" in payload:
        new_time = parse_time_input(payload["start_time"])
        if event.start_time != new_time:
            changes["start_time"] = (event.start_time, new_time)
            event.start_time = new_time

    if "notify" in payload:
        new_notify = bool(payload["notify"])
        if event.notify_start != new_notify:
            changes["notify_start"] = (event.notify_start, new_notify)
            event.notify_start = new_notify

    return changes


def create_ppr_event(db: Session, payload: dict, user) -> PprEvent:
    title = (payload.get("title") or "").strip()
    if not title:
        raise ValueError("title is required")

    event_date = parse_date_input(payload.get("date"))
    start_time = parse_time_input(payload.get("start_time"))
    project = (payload.get("project") or "").strip()
    try:
        if event_date is not None and start_time is not None and project:
            _acquire_create_duplicate_lock(
                db,
                title=title,
                project=project,
                event_date=event_date,
                start_time=start_time,
            )
            duplicate = find_active_ppr_duplicate(
                db,
                title=title,
                project=project,
                event_date=event_date,
                start_time=start_time,
            )
            if duplicate:
                raise PprDuplicateError(duplicate.id)

        now = datetime.utcnow()
        event = PprEvent(
            external_id=make_manual_external_id(),
            title=title,
            is_active=True,
            ppr_status=PPR_STATUS_SCHEDULED,
            is_manually_edited=True,
            manual_updated_at=now,
            updated_at=now,
        )
        db.add(event)
        db.flush()
        apply_event_payload(
            event,
            {
                **payload,
                "title": title,
                "project": project or payload.get("project"),
                "date": event_date,
                "start_time": start_time,
            },
        )
        add_event_audit(db, event, "created", user, "ППР создана вручную")
        sync_start_notification(db, event, user)
        db.commit()
        db.refresh(event)
        return get_event_card(db, event.id)
    except Exception:
        db.rollback()
        raise


def update_ppr_event(db: Session, event: PprEvent, payload: dict, user) -> PprEvent:
    if "title" in payload:
        title = (payload.get("title") or "").strip()
        if not title:
            raise ValueError("title is required")
        payload = {**payload, "title": title}
    changes = apply_event_payload(event, payload)
    event.is_manually_edited = True
    event.manual_updated_at = datetime.utcnow()
    event.updated_at = datetime.utcnow()
    if changes:
        add_event_audit(db, event, "updated", user, describe_changes(changes))
        if "date" in changes or "start_time" in changes:
            add_event_audit(db, event, "schedule_changed", user, describe_changes({k: v for k, v in changes.items() if k in {"date", "start_time"}}))
        if "notify_start" in changes:
            add_event_audit(db, event, "notification_setting_changed", user, describe_changes({"notify_start": changes["notify_start"]}))
    sync_start_notification(db, event, user, audit=bool(changes))
    db.commit()
    db.refresh(event)
    return get_event_card(db, event.id)


def archive_ppr_event(db: Session, event: PprEvent, user, force: bool = False) -> PprEvent:
    if not event.is_active or event.ppr_status == PPR_STATUS_ARCHIVED:
        return get_event_card(db, event.id)
    if event.ppr_status == PPR_STATUS_IN_PROGRESS and not force:
        raise WorkflowConflict("ППР находится в работе")
    event.is_active = False
    event.ppr_status = PPR_STATUS_ARCHIVED
    event.is_manually_edited = True
    event.manual_updated_at = datetime.utcnow()
    event.updated_at = datetime.utcnow()
    add_event_audit(db, event, "forced_archive" if force else "archived", user, "ППР принудительно архивирована" if force else "ППР отключена")
    sync_start_notification(db, event, user)
    db.commit()
    db.refresh(event)
    return get_event_card(db, event.id)


def restore_ppr_event(db: Session, event: PprEvent, user) -> PprEvent:
    event.is_active = True
    event.ppr_status = infer_restored_status(event)
    event.is_manually_edited = True
    event.manual_updated_at = datetime.utcnow()
    event.updated_at = datetime.utcnow()
    add_event_audit(db, event, "restored", user, "ППР восстановлена")
    sync_start_notification(db, event, user)
    db.commit()
    db.refresh(event)
    return get_event_card(db, event.id)


def take_notification(db: Session, notification_id: int, user_id: str, user_name: str) -> tuple[bool, str, PprNotification | None]:
    notif = get_notification(db, notification_id)
    if not notif:
        return False, "ППР не найдена", None
    event = notif.event
    if not event.is_active or event.ppr_status == PPR_STATUS_ARCHIVED:
        return False, "Архивную ППР нельзя взять в работу", notif
    if event.ppr_status == PPR_STATUS_VERIFIED:
        return False, "ППР уже проверена", notif
    if event.ppr_status == PPR_STATUS_IN_PROGRESS:
        if notif.taken_by_id == user_id:
            return True, "ППР уже находится у вас в работе", notif
        return False, f"ППР уже взял {notif.taken_by_name or 'другой пользователь'}", notif
    if event.ppr_status != PPR_STATUS_SCHEDULED:
        return False, f"Действие недоступно для статуса {event.ppr_status}", notif

    now = datetime.utcnow()
    result = db.execute(
        update(PprEvent)
        .where(PprEvent.id == notif.ppr_event_id)
        .where(PprEvent.is_active.is_(True))
        .where(PprEvent.ppr_status == PPR_STATUS_SCHEDULED)
        .values(ppr_status=PPR_STATUS_IN_PROGRESS, updated_at=now)
    )
    if result.rowcount != 1:
        db.rollback()
        fresh = get_notification(db, notification_id)
        if fresh and fresh.event.ppr_status == PPR_STATUS_IN_PROGRESS and fresh.taken_by_id == user_id:
            return True, "ППР уже находится у вас в работе", fresh
        if fresh and fresh.event.ppr_status == PPR_STATUS_IN_PROGRESS:
            return False, f"ППР уже взял {fresh.taken_by_name or 'другой пользователь'}", fresh
        if fresh and fresh.event.ppr_status == PPR_STATUS_VERIFIED:
            return False, "ППР уже проверена", fresh
        if fresh and (not fresh.event.is_active or fresh.event.ppr_status == PPR_STATUS_ARCHIVED):
            return False, "Архивную ППР нельзя взять в работу", fresh
        return False, "ППР уже изменила статус", fresh

    db.execute(
        update(PprNotification)
        .where(PprNotification.id == notification_id)
        .values(taken_by_id=user_id, taken_by_name=user_name, taken_at=now, updated_at=now)
    )
    db.add(AuditLog(notification_id=notification_id, ppr_event_id=notif.ppr_event_id, action="take", user_id=user_id, user_name=user_name))
    db.commit()
    return True, "Вы взяли ППР в работу", get_notification(db, notification_id)


def check_notification(db: Session, notification_id: int, user_id: str, user_name: str, is_admin: bool = False) -> tuple[bool, str, PprNotification | None]:
    notif = get_notification(db, notification_id)
    if not notif:
        return False, "ППР не найдена", None
    event = notif.event
    if not event.is_active or event.ppr_status == PPR_STATUS_ARCHIVED:
        return False, "Архивную ППР нельзя проверить", notif
    if event.ppr_status == PPR_STATUS_VERIFIED:
        return True, "ППР уже проверена", notif
    if event.ppr_status == PPR_STATUS_SCHEDULED or not notif.taken_by_id:
        return False, "Сначала возьмите ППР в работу", notif
    if event.ppr_status != PPR_STATUS_IN_PROGRESS:
        return False, f"Действие недоступно для статуса {event.ppr_status}", notif
    if not is_admin and notif.taken_by_id != user_id:
        return False, f"ППР находится в работе у {notif.taken_by_name or 'другого пользователя'}", notif

    now = datetime.utcnow()
    result = db.execute(
        update(PprEvent)
        .where(PprEvent.id == notif.ppr_event_id)
        .where(PprEvent.is_active.is_(True))
        .where(PprEvent.ppr_status == PPR_STATUS_IN_PROGRESS)
        .values(ppr_status=PPR_STATUS_VERIFIED, updated_at=now)
    )
    if result.rowcount != 1:
        db.rollback()
        fresh = get_notification(db, notification_id)
        if fresh and fresh.event.ppr_status == PPR_STATUS_VERIFIED:
            return True, "ППР уже проверена", fresh
        if fresh and fresh.event.ppr_status == PPR_STATUS_SCHEDULED:
            return False, "Сначала возьмите ППР в работу", fresh
        if fresh and (not fresh.event.is_active or fresh.event.ppr_status == PPR_STATUS_ARCHIVED):
            return False, "Архивную ППР нельзя проверить", fresh
        return False, "ППР уже изменила статус", fresh

    db.execute(
        update(PprNotification)
        .where(PprNotification.id == notification_id)
        .values(checked_by_id=user_id, checked_by_name=user_name, checked_at=now, updated_at=now)
    )
    db.add(AuditLog(notification_id=notification_id, ppr_event_id=notif.ppr_event_id, action="verify", user_id=user_id, user_name=user_name))
    db.commit()
    return True, "ППР отмечена как проверенная", get_notification(db, notification_id)


def add_comment(db: Session, notification_id: int, user_id: str, user_name: str, comment: str) -> tuple[bool, str]:
    notif = get_notification(db, notification_id)
    if not notif:
        return False, "ППР не найдена"
    db.add(AuditLog(notification_id=notification_id, ppr_event_id=notif.ppr_event_id, action="comment", user_id=user_id, user_name=user_name, comment=comment))
    db.commit()
    return True, "Комментарий сохранен"


def requeue_notification(
    db: Session,
    notification_id: int,
    scheduled_at: datetime,
    actor,
    *,
    force: bool = False,
) -> PprNotification:
    """Return one tested notification to the planned queue in one transaction."""
    if not actor or not actor.is_active or actor.role != ROLE_ADMIN:
        raise PermissionError("Требуются права admin")
    if scheduled_at <= datetime.now():
        raise ValueError("Новое время должно быть в будущем")

    notif = get_notification(db, notification_id)
    if not notif:
        raise LookupError("Уведомление не найдено")
    event = notif.event
    if not event:
        raise LookupError("Связанная ППР не найдена")
    if event.ppr_status in {PPR_STATUS_ARCHIVED, PPR_STATUS_CANCELLED} or not event.is_active:
        if not force:
            raise ValueError("Архивную или отменённую ППР можно вернуть только с FORCE")

    old_status = notif.status
    old_scheduled_at = notif.scheduled_at
    now = datetime.utcnow()

    event.is_active = True
    event.ppr_status = PPR_STATUS_SCHEDULED
    event.updated_at = now

    notif.status = NOTIFICATION_STATUS_PLANNED
    notif.scheduled_at = scheduled_at
    notif.auto_send_enabled = True
    notif.attempt_count = 0
    notif.last_error = None
    notif.sent_at = None
    notif.last_attempt_at = None
    notif.telegram_chat_id = None
    notif.telegram_message_id = None
    notif.processing_started_at = None
    notif.processing_by = None
    notif.processing_phase = None
    notif.telegram_edit_last_error = None
    notif.reminder_count = 0
    notif.last_reminder_at = None
    notif.taken_by_id = None
    notif.taken_by_name = None
    notif.taken_at = None
    notif.checked_by_id = None
    notif.checked_by_name = None
    notif.checked_at = None
    notif.updated_at = now

    comment = (
        f"old_status={old_status}; new_status={NOTIFICATION_STATUS_PLANNED}; "
        f"old_scheduled_at={old_scheduled_at.isoformat()}; new_scheduled_at={scheduled_at.isoformat()}"
    )
    db.add(AuditLog(
        notification_id=notif.id,
        ppr_event_id=event.id,
        action="notification_requeued",
        user_id=actor.telegram_id,
        user_name=actor.full_name or actor.username,
        comment=comment,
    ))
    db.commit()
    db.refresh(notif)
    return notif


def serialize_event(event: PprEvent) -> dict:
    return {
        "id": event.id,
        "external_id": event.external_id,
        "source_key": event.source_key,
        "date": event.date.isoformat() if event.date else None,
        "start_time": event.start_time.isoformat() if event.start_time else None,
        "end_time": event.end_time.isoformat() if event.end_time else None,
        "project": event.project,
        "title": event.title,
        "activities": [x.strip() for x in (event.activities or "").split(";") if x.strip()],
        "responsible_setup": event.responsible_setup,
        "responsible_report": event.responsible_report,
        "source_link": event.source_link,
        "outlook_link": event.outlook_link,
        "outlook_url": event.outlook_url,
        "notify_start": event.notify_start,
        "notify": event.notify_start,
        "is_active": event.is_active,
        "is_archived": not event.is_active,
        "ppr_status": event.ppr_status,
        "is_manually_edited": event.is_manually_edited,
        "manual_updated_at": event.manual_updated_at.isoformat() if event.manual_updated_at else None,
        "comment": event.comment,
    }


def serialize_audit_entries(entries: list[AuditLog]) -> list[dict]:
    return [
        {
            "action": a.action,
            "user_name": a.user_name,
            "comment": a.comment,
            "created_at": a.created_at.isoformat(),
        }
        for a in sorted(entries, key=lambda x: x.created_at)
    ]


def merge_audit_entries(*groups: list[AuditLog]) -> list[AuditLog]:
    result = []
    seen = set()
    for group in groups:
        for item in group:
            key = item.id if item.id is not None else id(item)
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
    return result


def serialize_event_card(event: PprEvent) -> dict:
    notification = sorted(event.notifications, key=lambda n: (n.scheduled_at, n.id))[0] if event.notifications else None
    requires_date = event.date is None
    status = PPR_STATUS_ARCHIVED if not event.is_active else event.ppr_status
    audit_entries = merge_audit_entries(list(event.audit_logs), list(notification.audit_logs) if notification else [])

    return {
        "notification_id": notification.id if notification else None,
        "type": notification.type if notification else None,
        "scheduled_at": notification.scheduled_at.isoformat() if notification else None,
        "status": status,
        "notification_status": notification.status if notification else None,
        "attempt_count": notification.attempt_count if notification else 0,
        "last_attempt_at": notification.last_attempt_at.isoformat() if notification and notification.last_attempt_at else None,
        "last_error": notification.last_error if notification else None,
        "processing_started_at": notification.processing_started_at.isoformat() if notification and notification.processing_started_at else None,
        "processing_by": notification.processing_by if notification else None,
        "processing_phase": notification.processing_phase if notification else None,
        "telegram_chat_id": notification.telegram_chat_id if notification else None,
        "telegram_message_id": notification.telegram_message_id if notification else None,
        "sent_at": notification.sent_at.isoformat() if notification and notification.sent_at else None,
        "requires_date": requires_date,
        "is_archived": not event.is_active,
        "taken_by_name": notification.taken_by_name if notification else None,
        "taken_at": notification.taken_at.isoformat() if notification and notification.taken_at else None,
        "checked_by_name": notification.checked_by_name if notification else None,
        "checked_at": notification.checked_at.isoformat() if notification and notification.checked_at else None,
        "event": serialize_event(event),
        "audit_log": serialize_audit_entries(audit_entries),
    }


def serialize_notification(notif: PprNotification) -> dict:
    e = notif.event
    return {
        "notification_id": notif.id,
        "type": notif.type,
        "scheduled_at": notif.scheduled_at.isoformat(),
        "status": PPR_STATUS_ARCHIVED if not e.is_active else e.ppr_status,
        "notification_status": notif.status,
        "attempt_count": notif.attempt_count,
        "last_attempt_at": notif.last_attempt_at.isoformat() if notif.last_attempt_at else None,
        "last_error": notif.last_error,
        "processing_started_at": notif.processing_started_at.isoformat() if notif.processing_started_at else None,
        "processing_by": notif.processing_by,
        "processing_phase": notif.processing_phase,
        "telegram_chat_id": notif.telegram_chat_id,
        "telegram_message_id": notif.telegram_message_id,
        "sent_at": notif.sent_at.isoformat() if notif.sent_at else None,
        "requires_date": e.date is None,
        "is_archived": not e.is_active,
        "taken_by_name": notif.taken_by_name,
        "taken_at": notif.taken_at.isoformat() if notif.taken_at else None,
        "checked_by_name": notif.checked_by_name,
        "checked_at": notif.checked_at.isoformat() if notif.checked_at else None,
        "event": serialize_event(e),
        "audit_log": serialize_audit_entries(merge_audit_entries(list(e.audit_logs), list(notif.audit_logs))),
    }
