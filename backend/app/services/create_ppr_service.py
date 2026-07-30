from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


CREATE_PPR_USAGE = (
    "Использование: /createppr YYYY-MM-DD HH:MM | Проект | "
    "Название ППР | Активности"
)
CREATE_PPR_PREVIEW_TTL = timedelta(minutes=10)
MAX_PROJECT_LENGTH = 255
MAX_TITLE_LENGTH = 500
MAX_ACTIVITIES_LENGTH = 2000

_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME_PATTERN = re.compile(r"\d{2}:\d{2}")


class CreatePprValidationError(ValueError):
    pass


class CreatePprPreviewNotFound(ValueError):
    pass


class CreatePprPreviewExpired(ValueError):
    pass


class CreatePprPreviewAccessDenied(ValueError):
    pass


class CreatePprPreviewCapacityExceeded(ValueError):
    pass


def normalize_text(value: str) -> str:
    return " ".join(value.strip().split())


@dataclass(frozen=True)
class CreatePprDraft:
    event_date: date
    start_time: time
    project: str
    title: str
    activities: str | None
    scheduled_at: datetime
    auto_send_enabled: bool

    def payload(self) -> dict:
        return {
            "date": self.event_date,
            "start_time": self.start_time,
            "project": self.project,
            "title": self.title,
            "activities": self.activities,
            "notify": True,
        }


@dataclass(frozen=True)
class CreatePprPreview:
    token: str
    telegram_user_id: str
    chat_id: str
    draft: CreatePprDraft
    created_at: datetime
    expires_at: datetime


def _configured_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise CreatePprValidationError(
            f"Неизвестный DEFAULT_TIMEZONE: {timezone_name}"
        ) from exc


def _localize_scheduled_at(
    scheduled_at: datetime,
    *,
    timezone_name: str,
    tz: ZoneInfo,
) -> datetime:
    candidates: dict[datetime, datetime] = {}
    for fold in (0, 1):
        candidate = scheduled_at.replace(tzinfo=tz, fold=fold)
        round_trip = (
            candidate.astimezone(timezone.utc)
            .astimezone(tz)
            .replace(tzinfo=None)
        )
        if round_trip == scheduled_at:
            candidates[candidate.astimezone(timezone.utc)] = candidate

    if not candidates:
        raise CreatePprValidationError(
            f"Указанное локальное время не существует в часовом поясе {timezone_name} "
            "из-за перехода на летнее/зимнее время."
        )
    if len(candidates) > 1:
        raise CreatePprValidationError(
            f"Указанное локальное время неоднозначно в часовом поясе {timezone_name} "
            "из-за перехода на летнее/зимнее время."
        )
    return next(iter(candidates.values()))


def parse_create_ppr_command(
    text: str | None,
    *,
    timezone_name: str,
    now: datetime | None = None,
) -> CreatePprDraft:
    command_text = (text or "").strip()
    _, separator, arguments = command_text.partition(" ")
    if not separator or not arguments.strip():
        raise CreatePprValidationError(CREATE_PPR_USAGE)

    fields = arguments.split("|")
    if len(fields) != 4:
        raise CreatePprValidationError(
            f"Ожидается ровно четыре поля, разделённых символом |. {CREATE_PPR_USAGE}"
        )

    schedule_text, project_text, title_text, activities_text = (
        field.strip() for field in fields
    )
    schedule_parts = schedule_text.split()
    if len(schedule_parts) != 2:
        raise CreatePprValidationError(
            f"Дата и время должны иметь формат YYYY-MM-DD HH:MM. {CREATE_PPR_USAGE}"
        )
    date_text, time_text = schedule_parts
    if not _DATE_PATTERN.fullmatch(date_text):
        raise CreatePprValidationError("Дата должна иметь строгий формат YYYY-MM-DD.")
    if not _TIME_PATTERN.fullmatch(time_text):
        raise CreatePprValidationError("Время должно иметь строгий формат HH:MM.")

    try:
        event_date = date.fromisoformat(date_text)
    except ValueError as exc:
        raise CreatePprValidationError("Указана несуществующая календарная дата.") from exc
    try:
        hour, minute = (int(part) for part in time_text.split(":"))
        start_time = time(hour=hour, minute=minute)
    except (TypeError, ValueError) as exc:
        raise CreatePprValidationError("Время должно находиться в диапазоне 00:00–23:59.") from exc

    project = normalize_text(project_text)
    title = normalize_text(title_text)
    activities = normalize_text(activities_text) or None
    if not project:
        raise CreatePprValidationError("Проект не может быть пустым.")
    if not title:
        raise CreatePprValidationError("Название ППР не может быть пустым.")
    if len(project) > MAX_PROJECT_LENGTH:
        raise CreatePprValidationError(
            f"Проект не может быть длиннее {MAX_PROJECT_LENGTH} символов."
        )
    if len(title) > MAX_TITLE_LENGTH:
        raise CreatePprValidationError(
            f"Название ППР не может быть длиннее {MAX_TITLE_LENGTH} символов."
        )
    if activities and len(activities) > MAX_ACTIVITIES_LENGTH:
        raise CreatePprValidationError(
            f"Активности не могут быть длиннее {MAX_ACTIVITIES_LENGTH} символов."
        )

    tz = _configured_timezone(timezone_name)
    current = now or datetime.now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    else:
        current = current.astimezone(tz)
    draft = CreatePprDraft(
        event_date=event_date,
        start_time=start_time,
        project=project,
        title=title,
        activities=activities,
        scheduled_at=datetime.combine(event_date, start_time),
        auto_send_enabled=True,
    )
    ensure_create_ppr_draft_is_future(
        draft,
        timezone_name=timezone_name,
        now=current,
    )
    return draft


def ensure_create_ppr_draft_is_future(
    draft: CreatePprDraft,
    *,
    timezone_name: str,
    now: datetime | None = None,
) -> None:
    tz = _configured_timezone(timezone_name)
    current = now or datetime.now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    else:
        current = current.astimezone(tz)
    scheduled_aware = _localize_scheduled_at(
        draft.scheduled_at,
        timezone_name=timezone_name,
        tz=tz,
    )
    if scheduled_aware <= current:
        raise CreatePprValidationError(
            f"Дата и время ППР должны быть в будущем ({timezone_name})."
        )


class CreatePprPreviewStore:
    def __init__(
        self,
        *,
        ttl: timedelta = CREATE_PPR_PREVIEW_TTL,
        max_entries: int = 1000,
    ):
        self.ttl = ttl
        self.max_entries = max(1, max_entries)
        self._previews: dict[str, CreatePprPreview] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _utc_now(now: datetime | None = None) -> datetime:
        value = now or datetime.now(timezone.utc)
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _cleanup_locked(self, now: datetime) -> None:
        expired = [
            token
            for token, preview in self._previews.items()
            if preview.expires_at <= now
        ]
        for token in expired:
            self._previews.pop(token, None)

    def create(
        self,
        draft: CreatePprDraft,
        *,
        telegram_user_id: str,
        chat_id: str,
        now: datetime | None = None,
    ) -> CreatePprPreview:
        current = self._utc_now(now)
        with self._lock:
            self._cleanup_locked(current)
            if len(self._previews) >= self.max_entries:
                raise CreatePprPreviewCapacityExceeded(
                    "Слишком много активных Preview. Подтвердите или отмените ранее созданный Preview либо повторите позже."
                )
            token = uuid4().hex
            while token in self._previews:
                token = uuid4().hex
            preview = CreatePprPreview(
                token=token,
                telegram_user_id=str(telegram_user_id),
                chat_id=str(chat_id),
                draft=draft,
                created_at=current,
                expires_at=current + self.ttl,
            )
            self._previews[token] = preview
            return preview

    def consume(
        self,
        token: str,
        *,
        telegram_user_id: str,
        chat_id: str,
        now: datetime | None = None,
    ) -> CreatePprPreview:
        current = self._utc_now(now)
        with self._lock:
            preview = self._previews.get(token)
            if preview is None:
                self._cleanup_locked(current)
                raise CreatePprPreviewNotFound(
                    "Preview не найден или уже был использован."
                )
            if preview.expires_at <= current:
                self._previews.pop(token, None)
                self._cleanup_locked(current)
                raise CreatePprPreviewExpired("Срок действия Preview истёк.")
            if (
                preview.telegram_user_id != str(telegram_user_id)
                or preview.chat_id != str(chat_id)
            ):
                raise CreatePprPreviewAccessDenied(
                    "Подтвердить Preview может только создавший его admin в исходном чате."
                )
            self._previews.pop(token, None)
            self._cleanup_locked(current)
            return preview

    def cancel(
        self,
        token: str,
        *,
        telegram_user_id: str,
        chat_id: str,
        now: datetime | None = None,
    ) -> CreatePprPreview:
        return self.consume(
            token,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            now=now,
        )

    def __len__(self) -> int:
        with self._lock:
            self._cleanup_locked(self._utc_now())
            return len(self._previews)
