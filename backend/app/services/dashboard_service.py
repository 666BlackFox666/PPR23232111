from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from sqlalchemy import and_, asc, case, desc, exists, func, or_
from sqlalchemy.orm import Session, selectinload

from app.db.models import PprEvent, PprNotification
from app.services.ppr_service import serialize_event_card
from app.services.statuses import (
    NOTIFICATION_STATUS_FAILED,
    PPR_STATUS_ARCHIVED,
    PPR_STATUS_CANCELLED,
    PPR_STATUS_IN_PROGRESS,
    PPR_STATUS_SCHEDULED,
    PPR_STATUS_VERIFIED,
)


def normalize_search(value: str | None) -> list[str]:
    if not value:
        return []
    normalized = re.sub(r"\s+", " ", value.strip().lower())
    return [part for part in normalized.split(" ") if part]


def scheduled_datetime(event: PprEvent) -> datetime | None:
    if event.date is None or event.start_time is None:
        return None
    return datetime.combine(event.date, event.start_time)


def is_overdue_event(event: PprEvent, now: datetime | None = None) -> bool:
    if now is None:
        now = datetime.now()
    if not event.is_active or event.ppr_status in {PPR_STATUS_VERIFIED, PPR_STATUS_ARCHIVED, PPR_STATUS_CANCELLED}:
        return False
    scheduled_at = scheduled_datetime(event)
    return bool(scheduled_at and scheduled_at < now)


def base_event_query(db: Session, *, load_related: bool = True):
    """Build an event-only query.

    Filters that refer to notifications use correlated EXISTS expressions below.
    Keeping the primary query on ppr_events avoids duplicate event rows and makes
    pagination/sorting valid on PostgreSQL without DISTINCT.
    """
    query = db.query(PprEvent)
    if load_related:
        query = query.options(
            selectinload(PprEvent.notifications).selectinload(PprNotification.audit_logs),
            selectinload(PprEvent.audit_logs),
        )
    return query


def notification_exists(*conditions):
    return exists().where(PprNotification.ppr_event_id == PprEvent.id).where(*conditions)


def apply_search(query, search: str | None):
    tokens = normalize_search(search)
    if not tokens:
        return query
    for token in tokens:
        pattern = f"%{token}%"
        notification_match = notification_exists(
            or_(
                func.lower(func.coalesce(PprNotification.taken_by_name, "")).like(pattern),
                func.lower(func.coalesce(PprNotification.checked_by_name, "")).like(pattern),
            )
        )
        query = query.filter(
            or_(
                func.lower(PprEvent.title).like(pattern),
                func.lower(func.coalesce(PprEvent.project, "")).like(pattern),
                func.lower(func.coalesce(PprEvent.activities, "")).like(pattern),
                func.lower(func.coalesce(PprEvent.responsible_setup, "")).like(pattern),
                func.lower(func.coalesce(PprEvent.responsible_report, "")).like(pattern),
                func.lower(func.coalesce(PprEvent.comment, "")).like(pattern),
                notification_match,
            )
        )
    return query


def apply_event_filters(
    query,
    *,
    status: str | None = None,
    project: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    date_state: str | None = None,
    checker: str | None = None,
    notify: bool | None = None,
    outlook: bool | None = None,
    include_archived: bool = False,
    quick_filter: str | None = None,
    current_user_id: str | None = None,
):
    if status:
        if status == PPR_STATUS_ARCHIVED:
            query = query.filter(or_(PprEvent.ppr_status == PPR_STATUS_ARCHIVED, PprEvent.is_active.is_(False)))
        else:
            query = query.filter(PprEvent.ppr_status == status)

    if not include_archived and status != PPR_STATUS_ARCHIVED:
        query = query.filter(PprEvent.is_active.is_(True))

    if project:
        query = query.filter(func.lower(func.coalesce(PprEvent.project, "")).like(f"%{project.strip().lower()}%"))
    if date_from:
        query = query.filter(PprEvent.date >= date_from)
    if date_to:
        query = query.filter(PprEvent.date <= date_to)
    if date_state == "missing":
        query = query.filter(PprEvent.date.is_(None))
    elif date_state == "present":
        query = query.filter(PprEvent.date.isnot(None))
    if notify is not None:
        query = query.filter(PprEvent.notify_start.is_(notify))
    if outlook is True:
        query = query.filter(PprEvent.outlook_link.isnot(None), PprEvent.outlook_link != "")
    elif outlook is False:
        query = query.filter(or_(PprEvent.outlook_link.is_(None), PprEvent.outlook_link == ""))

    if checker:
        query = query.filter(
            notification_exists(
                or_(PprNotification.taken_by_id == checker, PprNotification.taken_by_name == checker)
            )
        )
    elif quick_filter == "mine_in_progress" and current_user_id:
        query = query.filter(
            PprEvent.ppr_status == PPR_STATUS_IN_PROGRESS,
            notification_exists(PprNotification.taken_by_id == current_user_id),
        )

    today = date.today()
    if quick_filter == "today":
        query = query.filter(PprEvent.date == today)
    elif quick_filter == "missing_date":
        query = query.filter(PprEvent.date.is_(None))
    elif quick_filter == "unverified":
        query = query.filter(PprEvent.ppr_status.in_([PPR_STATUS_SCHEDULED, PPR_STATUS_IN_PROGRESS]))
    elif quick_filter == "overdue":
        now = datetime.now()
        query = query.filter(
            PprEvent.date.isnot(None),
            PprEvent.start_time.isnot(None),
            PprEvent.ppr_status.notin_([PPR_STATUS_VERIFIED, PPR_STATUS_ARCHIVED, PPR_STATUS_CANCELLED]),
            or_(PprEvent.date < now.date(), (PprEvent.date == now.date()) & (PprEvent.start_time < now.time())),
        )
    elif quick_filter == "notification_errors":
        query = query.filter(notification_exists(PprNotification.status == NOTIFICATION_STATUS_FAILED))

    return query


def apply_sort(query, sort: str):
    if sort == "date_desc":
        return query.order_by(desc(PprEvent.date).nullslast(), desc(PprEvent.start_time).nullslast(), desc(PprEvent.id))
    if sort == "overdue_first":
        now = datetime.now()
        overdue_rank = case(
            (
                and_(
                    PprEvent.is_active.is_(True),
                    PprEvent.date.isnot(None),
                    PprEvent.start_time.isnot(None),
                    PprEvent.ppr_status.notin_([PPR_STATUS_VERIFIED, PPR_STATUS_ARCHIVED, PPR_STATUS_CANCELLED]),
                    or_(
                        PprEvent.date < now.date(),
                        (PprEvent.date == now.date()) & (PprEvent.start_time < now.time()),
                    ),
                ),
                0,
            ),
            else_=1,
        )
        return query.order_by(asc(overdue_rank), asc(PprEvent.date).nullslast(), asc(PprEvent.start_time).nullslast(), asc(PprEvent.id))
    if sort == "updated_desc":
        return query.order_by(desc(PprEvent.updated_at), desc(PprEvent.id))
    if sort == "title":
        return query.order_by(asc(func.lower(PprEvent.title)), asc(PprEvent.id))
    if sort == "project":
        return query.order_by(asc(func.lower(func.coalesce(PprEvent.project, ""))), asc(func.lower(PprEvent.title)), asc(PprEvent.id))
    return query.order_by(asc(PprEvent.date).nullslast(), asc(PprEvent.start_time).nullslast(), asc(PprEvent.id))


def list_ppr_events(
    db: Session,
    *,
    search: str | None = None,
    status: str | None = None,
    project: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    date_state: str | None = None,
    checker: str | None = None,
    notify: bool | None = None,
    outlook: bool | None = None,
    include_archived: bool = False,
    sort: str = "date_asc",
    page: int = 1,
    page_size: int = 25,
    quick_filter: str | None = None,
    current_user_id: str | None = None,
) -> dict[str, Any]:
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)

    query = base_event_query(db, load_related=False)
    query = apply_search(query, search)
    query = apply_event_filters(
        query,
        status=status,
        project=project,
        date_from=date_from,
        date_to=date_to,
        date_state=date_state,
        checker=checker,
        notify=notify,
        outlook=outlook,
        include_archived=include_archived,
        quick_filter=quick_filter,
        current_user_id=current_user_id,
    )
    total = query.order_by(None).with_entities(func.count(PprEvent.id)).scalar() or 0
    id_query = query.with_entities(PprEvent.id)
    page_ids = [item[0] for item in apply_sort(id_query, sort).offset((page - 1) * page_size).limit(page_size).all()]
    if page_ids:
        events_by_id = {event.id: event for event in base_event_query(db).filter(PprEvent.id.in_(page_ids)).all()}
        events = [events_by_id[event_id] for event_id in page_ids if event_id in events_by_id]
    else:
        events = []

    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "items": [serialize_event_card(event) for event in events],
    }


def dashboard_summary(db: Session, include_admin_counts: bool = False) -> dict[str, int]:
    today = date.today()
    events = base_event_query(db).all()
    active = [event for event in events if event.is_active]
    summary = {
        "today": sum(1 for event in active if event.date == today),
        "scheduled": sum(1 for event in active if event.ppr_status == PPR_STATUS_SCHEDULED),
        "in_progress": sum(1 for event in active if event.ppr_status == PPR_STATUS_IN_PROGRESS),
        "verified": sum(1 for event in active if event.ppr_status == PPR_STATUS_VERIFIED),
        "overdue": sum(1 for event in active if is_overdue_event(event)),
        "missing_date": sum(1 for event in active if event.date is None),
        "notification_errors": db.query(PprNotification).join(PprNotification.event).filter(PprEvent.is_active.is_(True), PprNotification.status == NOTIFICATION_STATUS_FAILED).count(),
        "archived": 0,
    }
    if include_admin_counts:
        summary["archived"] = sum(1 for event in events if not event.is_active or event.ppr_status == PPR_STATUS_ARCHIVED)
    return summary
