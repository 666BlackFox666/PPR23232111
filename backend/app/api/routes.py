from datetime import date
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.db.session import get_db
from app.db.models import AppUser, PprEvent, PprNotification
from app.excel.importer import import_excel
from app.excel.import_service import (
    ImportFileChanged,
    ImportForceConfirmationRequired,
    ImportPreviewError,
    ImportPreviewNotFound,
    ImportRepeatedFile,
    apply_saved_import_preview,
    read_file_bytes,
    save_import_preview,
)
from app.services.outlook_graph import (
    OutlookGraphConfigError,
    OutlookGraphDisabledError,
    OutlookGraphNoDateError,
    OutlookGraphRequestError,
    outlook_integration_configured,
    sync_notification_outlook_link,
)
from app.services.ppr_service import (
    WorkflowConflict,
    add_comment,
    archive_ppr_event,
    check_notification,
    create_ppr_event,
    get_event_card,
    get_notification,
    restore_ppr_event,
    serialize_event_card,
    serialize_notification,
    take_notification,
    update_ppr_event,
)
from app.services.statuses import (
    NOTIFICATION_STATUS_CANCELLED,
    NOTIFICATION_STATUS_SKIPPED,
    PPR_STATUS_IN_PROGRESS,
    PPR_STATUS_SCHEDULED,
)
from app.services.telegram_sender import (
    build_autosend_preview,
    list_notifications,
    mark_unknown_notification_sent,
    retry_failed_notification,
    retry_unknown_notification,
    scheduler_status,
)
from app.services.dashboard_service import dashboard_summary, list_ppr_events
from app.api.auth import get_current_user, require_roles
from app.services.user_service import (
    ROLE_ADMIN,
    ROLE_CHECKER,
    create_user,
    serialize_user,
    update_user,
    user_display,
)

router = APIRouter()
settings = get_settings()


class ActionPayload(BaseModel):
    comment: str | None = None


class UserCreatePayload(BaseModel):
    telegram_id: str
    username: str | None = None
    full_name: str | None = None
    role: str = ROLE_CHECKER
    is_active: bool = True


class UserUpdatePayload(BaseModel):
    username: str | None = None
    full_name: str | None = None
    role: str | None = None
    is_active: bool | None = None


class PprPayload(BaseModel):
    title: str | None = None
    project: str | None = None
    date: str | None = None
    start_time: str | None = None
    activities: str | None = None
    notify: bool | None = None
    outlook_link: str | None = None
    comment: str | None = None


def payload_dict(payload: PprPayload) -> dict:
    return payload.model_dump(exclude_unset=True)


async def read_import_upload(file: UploadFile | None) -> tuple[str, bytes]:
    if file is None:
        return read_file_bytes(settings.schedule_xlsx_path)
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Excel file is empty")
    return file.filename or "schedule.xlsx", content


@router.get("/health")
def health():
    return {"ok": True}

@router.get("/api/capabilities")
def api_capabilities(user: AppUser = Depends(get_current_user)):
    return {"features": {"outlook": outlook_integration_configured()}}


@router.post("/api/import/excel")
async def import_excel_endpoint(db: Session = Depends(get_db), user: AppUser = Depends(require_roles(ROLE_ADMIN))):
    result = await import_excel(db, settings.schedule_xlsx_path)
    return result.__dict__


@router.post("/api/import/excel/preview")
async def import_excel_preview_endpoint(
    mode: str = Form("safe"),
    file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_roles(ROLE_ADMIN)),
):
    filename, content = await read_import_upload(file)
    try:
        return save_import_preview(db, content, filename, mode, user)
    except ImportPreviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/import/excel/apply")
async def import_excel_apply_endpoint(
    preview_id: str = Form(""),
    mode: str = Form("safe"),
    confirm_force: bool = Form(False),
    file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_roles(ROLE_ADMIN)),
):
    if not preview_id:
        raise HTTPException(status_code=409, detail="Preview is required. Run /api/import/excel/preview first.")
    filename, content = await read_import_upload(file)
    try:
        return await apply_saved_import_preview(db, preview_id, content, mode, user, confirm_force=confirm_force)
    except ImportPreviewNotFound as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ImportFileChanged as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ImportRepeatedFile as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ImportForceConfirmationRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ImportPreviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/ppr/today")
def ppr_today(db: Session = Depends(get_db), user: AppUser = Depends(require_roles(ROLE_ADMIN, ROLE_CHECKER))):
    today = date.today()
    notifications = (
        db.query(PprNotification)
        .options(joinedload(PprNotification.event).joinedload(PprEvent.audit_logs), joinedload(PprNotification.audit_logs))
        .join(PprNotification.event)
        .filter(PprEvent.date == today)
        .filter(PprEvent.is_active.is_(True))
        .filter(PprNotification.status.notin_([NOTIFICATION_STATUS_SKIPPED, NOTIFICATION_STATUS_CANCELLED]))
        .order_by(PprNotification.scheduled_at.asc())
        .all()
    )
    return [serialize_notification(n) for n in notifications]


@router.get("/api/ppr/all")
def ppr_all(
    include_archived: bool = False,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_roles(ROLE_ADMIN, ROLE_CHECKER)),
):
    query = (
        db.query(PprEvent)
        .options(joinedload(PprEvent.notifications).joinedload(PprNotification.audit_logs), joinedload(PprEvent.audit_logs))
    )
    if not include_archived:
        query = query.filter(PprEvent.is_active.is_(True))
    events = query.order_by(PprEvent.source_row.asc(), PprEvent.id.asc()).limit(1000).all()
    return [serialize_event_card(e) for e in events]


@router.get("/api/ppr/unverified")
def ppr_unverified(db: Session = Depends(get_db), user: AppUser = Depends(require_roles(ROLE_ADMIN, ROLE_CHECKER))):
    notifications = (
        db.query(PprNotification)
        .options(joinedload(PprNotification.event).joinedload(PprEvent.audit_logs), joinedload(PprNotification.audit_logs))
        .join(PprNotification.event)
        .filter(PprEvent.is_active.is_(True))
        .filter(PprEvent.date.isnot(None))
        .filter(PprEvent.ppr_status.in_([PPR_STATUS_SCHEDULED, PPR_STATUS_IN_PROGRESS]))
        .filter(PprNotification.status.notin_([NOTIFICATION_STATUS_SKIPPED, NOTIFICATION_STATUS_CANCELLED]))
        .order_by(PprNotification.scheduled_at.asc())
        .limit(500)
        .all()
    )
    return [serialize_notification(n) for n in notifications]


@router.get("/api/ppr/missing-date")
def ppr_missing_date(db: Session = Depends(get_db), user: AppUser = Depends(require_roles(ROLE_ADMIN, ROLE_CHECKER))):
    events = (
        db.query(PprEvent)
        .options(joinedload(PprEvent.notifications).joinedload(PprNotification.audit_logs), joinedload(PprEvent.audit_logs))
        .filter(PprEvent.is_active.is_(True))
        .filter(PprEvent.date.is_(None))
        .order_by(PprEvent.source_row.asc(), PprEvent.id.asc())
        .limit(500)
        .all()
    )
    return [serialize_event_card(e) for e in events]


@router.get("/api/dashboard/summary")
def api_dashboard_summary(db: Session = Depends(get_db), user: AppUser = Depends(require_roles(ROLE_ADMIN, ROLE_CHECKER))):
    return dashboard_summary(db, include_admin_counts=user.role == ROLE_ADMIN)


@router.get("/api/ppr")
def api_ppr_list(
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
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    quick_filter: str | None = None,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_roles(ROLE_ADMIN, ROLE_CHECKER)),
):
    return list_ppr_events(
        db,
        search=search,
        status=status,
        project=project,
        date_from=date_from,
        date_to=date_to,
        date_state=date_state,
        checker=checker,
        notify=notify,
        outlook=outlook,
        include_archived=include_archived and user.role == ROLE_ADMIN,
        sort=sort,
        page=page,
        page_size=page_size,
        quick_filter=quick_filter,
        current_user_id=user.telegram_id,
    )


@router.get("/api/ppr/{ppr_id}")
def ppr_event_card(ppr_id: int, db: Session = Depends(get_db), user: AppUser = Depends(require_roles(ROLE_ADMIN, ROLE_CHECKER))):
    event = get_event_card(db, ppr_id)
    if not event:
        raise HTTPException(status_code=404, detail="PPR event not found")
    return serialize_event_card(event)


@router.post("/api/ppr")
def api_create_ppr(payload: PprPayload, db: Session = Depends(get_db), user: AppUser = Depends(require_roles(ROLE_ADMIN))):
    try:
        event = create_ppr_event(db, payload_dict(payload), user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return serialize_event_card(event)


@router.patch("/api/ppr/{ppr_id}")
def api_update_ppr(ppr_id: int, payload: PprPayload, db: Session = Depends(get_db), user: AppUser = Depends(require_roles(ROLE_ADMIN))):
    event = get_event_card(db, ppr_id)
    if not event:
        raise HTTPException(status_code=404, detail="PPR event not found")
    try:
        updated = update_ppr_event(db, event, payload_dict(payload), user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return serialize_event_card(updated)


@router.post("/api/ppr/{ppr_id}/archive")
def api_archive_ppr(ppr_id: int, force: bool = False, db: Session = Depends(get_db), user: AppUser = Depends(require_roles(ROLE_ADMIN))):
    event = get_event_card(db, ppr_id)
    if not event:
        raise HTTPException(status_code=404, detail="PPR event not found")
    try:
        return serialize_event_card(archive_ppr_event(db, event, user, force=force))
    except WorkflowConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/ppr/{ppr_id}/restore")
def api_restore_ppr(ppr_id: int, db: Session = Depends(get_db), user: AppUser = Depends(require_roles(ROLE_ADMIN))):
    event = get_event_card(db, ppr_id)
    if not event:
        raise HTTPException(status_code=404, detail="PPR event not found")
    return serialize_event_card(restore_ppr_event(db, event, user))


@router.get("/api/notifications/autosend-preview")
def api_autosend_preview(
    respect_global_enabled: bool = False,
    limit: int = 10,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_roles(ROLE_ADMIN)),
):
    return build_autosend_preview(db, limit=limit, respect_global_enabled=respect_global_enabled)


@router.get("/api/notifications")
def api_notifications(
    status: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    ppr_event_id: int | None = None,
    failed_only: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_roles(ROLE_ADMIN)),
):
    result = list_notifications(
        db,
        status=status,
        date_from=date_from,
        date_to=date_to,
        ppr_event_id=ppr_event_id,
        failed_only=failed_only,
        page=page,
        page_size=page_size,
    )
    return {
        **{key: value for key, value in result.items() if key != "items"},
        "items": [serialize_notification(item) for item in result["items"]],
    }


@router.post("/api/notifications/{notification_id}/retry")
def api_retry_notification(
    notification_id: int,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_roles(ROLE_ADMIN)),
):
    current = get_notification(db, notification_id)
    if not current:
        raise HTTPException(status_code=404, detail="Notification not found")
    if current.status != "failed":
        raise HTTPException(status_code=409, detail=f"Notification has status {current.status}; only failed can be retried")
    notif = retry_failed_notification(db, notification_id)
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    return serialize_notification(notif)


@router.post("/api/notifications/{notification_id}/mark-sent")
def api_mark_unknown_sent(
    notification_id: int,
    payload: ActionPayload,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_roles(ROLE_ADMIN)),
):
    if not payload.comment or not payload.comment.strip():
        raise HTTPException(status_code=400, detail="Comment is required")
    current = get_notification(db, notification_id)
    if not current:
        raise HTTPException(status_code=404, detail="Notification not found")
    if current.status not in {"delivery_unknown", "sent"}:
        raise HTTPException(status_code=409, detail=f"Notification has status {current.status}; only delivery_unknown can be marked sent")
    notif = mark_unknown_notification_sent(db, notification_id, user, payload.comment.strip())
    return serialize_notification(notif)


@router.post("/api/notifications/{notification_id}/retry-unknown")
def api_retry_unknown(
    notification_id: int,
    confirm: bool = False,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_roles(ROLE_ADMIN)),
):
    current = get_notification(db, notification_id)
    if not current:
        raise HTTPException(status_code=404, detail="Notification not found")
    if current.status != "delivery_unknown":
        raise HTTPException(status_code=409, detail=f"Notification has status {current.status}; only delivery_unknown can be retried here")
    try:
        notif = retry_unknown_notification(db, notification_id, user, confirm)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return serialize_notification(notif)


@router.get("/api/notifications/{notification_id}")
def ppr_card(notification_id: int, db: Session = Depends(get_db), user: AppUser = Depends(require_roles(ROLE_ADMIN, ROLE_CHECKER))):
    notif = get_notification(db, notification_id)
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    return serialize_notification(notif)


@router.get("/api/system/scheduler-status")
def api_scheduler_status(db: Session = Depends(get_db), user: AppUser = Depends(require_roles(ROLE_ADMIN))):
    return scheduler_status(db)


@router.post("/api/notifications/{notification_id}/take")
def api_take(notification_id: int, db: Session = Depends(get_db), user: AppUser = Depends(require_roles(ROLE_ADMIN, ROLE_CHECKER))):
    ok, message, notif = take_notification(db, notification_id, user.telegram_id, user_display(user))
    if not notif:
        raise HTTPException(status_code=404, detail=message)
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    return serialize_notification(notif)


@router.post("/api/notifications/{notification_id}/check")
def api_check(notification_id: int, db: Session = Depends(get_db), user: AppUser = Depends(require_roles(ROLE_ADMIN, ROLE_CHECKER))):
    ok, message, notif = check_notification(db, notification_id, user.telegram_id, user_display(user), is_admin=user.role == ROLE_ADMIN)
    if not notif:
        raise HTTPException(status_code=404, detail=message)
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    return serialize_notification(notif)


@router.post("/api/notifications/{notification_id}/comment")
def api_comment(
    notification_id: int,
    payload: ActionPayload,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_roles(ROLE_ADMIN, ROLE_CHECKER)),
):
    ok, message = add_comment(db, notification_id, user.telegram_id, user_display(user), payload.comment or "")
    if not ok:
        raise HTTPException(status_code=404, detail=message)
    return {"ok": True, "message": message}


@router.post("/api/outlook/sync/{notification_id}")
async def api_outlook_sync(notification_id: int, db: Session = Depends(get_db), user: AppUser = Depends(require_roles(ROLE_ADMIN))):
    if not outlook_integration_configured():
        raise HTTPException(status_code=400, detail="Outlook-интеграция отключена или не настроена")

    notif = get_notification(db, notification_id)
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")

    try:
        link = await sync_notification_outlook_link(db, notif)
    except OutlookGraphDisabledError as exc:
        raise HTTPException(status_code=400, detail="Outlook disabled: OUTLOOK_ENABLED=false") from exc
    except OutlookGraphNoDateError as exc:
        raise HTTPException(status_code=400, detail="PPR event has no date") from exc
    except OutlookGraphConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OutlookGraphRequestError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not link:
        return {
            "ok": False,
            "notification_id": notification_id,
            "ppr_event_id": notif.ppr_event_id,
            "outlook_link": None,
            "outlook_url": None,
            "message": "Outlook event not found",
        }

    return {
        "ok": True,
        "notification_id": notification_id,
        "ppr_event_id": notif.ppr_event_id,
        "outlook_link": link,
        "outlook_url": link,
        "message": "Outlook link saved",
    }


@router.get("/api/me")
def api_me(user: AppUser = Depends(get_current_user)):
    return serialize_user(user)


@router.get("/api/users")
def api_users(db: Session = Depends(get_db), user: AppUser = Depends(require_roles(ROLE_ADMIN))):
    users = db.query(AppUser).order_by(AppUser.id.asc()).all()
    return [serialize_user(item) for item in users]


@router.post("/api/users")
def api_create_user(
    payload: UserCreatePayload,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_roles(ROLE_ADMIN)),
):
    try:
        created = create_user(db, payload.telegram_id, payload.username, payload.full_name, payload.role, payload.is_active, actor=user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return serialize_user(created)


@router.patch("/api/users/{user_id}")
def api_update_user(
    user_id: int,
    payload: UserUpdatePayload,
    db: Session = Depends(get_db),
    current_user: AppUser = Depends(require_roles(ROLE_ADMIN)),
):
    user = db.get(AppUser, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        updated = update_user(db, user, payload.username, payload.full_name, payload.role, payload.is_active, actor=current_user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return serialize_user(updated)
