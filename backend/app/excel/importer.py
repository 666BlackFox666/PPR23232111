from dataclasses import dataclass
from datetime import datetime, date, time
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from sqlalchemy.orm import Session

from app.db.models import PprEvent, PprNotification, AuditLog


@dataclass
class ImportResult:
    imported_events: int = 0
    created_notifications: int = 0
    skipped_without_date: int = 0
    errors: list[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


HEADER_ALIASES = {
    "ID": "external_id",
    "Дата": "date",
    "Время выхода": "start_time",
    "Время завершения": "end_time",
    "Тип уведомления": "notification_type",
    "Проект": "project",
    "Название ППР": "title",
    "Активности": "activities",
    "Ответственный настройка": "responsible_setup",
    "Ответственный отчетка": "responsible_report",
    "Ссылка": "source_link",
    "Уведомлять о выходе": "notify_start",
    "Уведомлять о завершении": "notify_end",
    "Активно": "is_active",
    "Комментарий": "comment",
    "Исходная строка": "source_row",
}


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"да", "yes", "y", "true", "1", "истина"}


def parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Не удалось прочитать дату: {value}")


def parse_time(value: Any, default: time | None = None) -> time | None:
    if value is None or value == "":
        return default
    if isinstance(value, datetime):
        return value.time().replace(microsecond=0)
    if isinstance(value, time):
        return value.replace(microsecond=0)
    text = str(value).strip()
    for fmt in ("%H:%M:%S", "%H:%M", "%H.%M.%S", "%H.%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            pass
    raise ValueError(f"Не удалось прочитать время: {value}")


def combine_datetime(d: date | None, t: time | None) -> datetime | None:
    if d is None or t is None:
        return None
    return datetime.combine(d, t)


def get_header_map(ws) -> dict[str, int]:
    result = {}
    for idx, cell in enumerate(ws[1], start=1):
        if cell.value in HEADER_ALIASES:
            result[HEADER_ALIASES[cell.value]] = idx
    return result


def cell(row, header_map: dict[str, int], key: str) -> Any:
    col = header_map.get(key)
    return row[col - 1].value if col else None


async def import_excel(db: Session, path: str | Path, sheet_name: str = "ППР_для_бота") -> ImportResult:
    from types import SimpleNamespace

    from app.excel.import_service import ImportRepeatedFile, apply_saved_import_preview, read_file_bytes, save_import_preview

    filename, content = read_file_bytes(path)
    user = SimpleNamespace(telegram_id="system", username="system", full_name="system")
    preview = save_import_preview(db, content, filename, "safe", user)
    if preview.get("warnings"):
        summary = preview["summary"]
        details = preview["details"]
    else:
        try:
            applied = await apply_saved_import_preview(db, preview["preview_id"], content, "safe", user)
            summary = applied["summary"]
            details = applied["details"]
        except ImportRepeatedFile:
            summary = preview["summary"]
            details = preview["details"]
    return ImportResult(
        imported_events=summary["valid_rows"],
        created_notifications=summary["notifications_to_create"],
        skipped_without_date=summary["missing_date_rows"],
        errors=[item["reason"] for item in details if item["action"] == "invalid"],
    )
