import asyncio
import os
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fastapi import HTTPException

from app.api.auth import require_roles
from app.config import get_settings
from app.db.models import AppUser, ImportRun, PprEvent, PprNotification
from app.db.session import Base
from app.excel.import_service import (
    IMPORT_MODE_FORCE,
    IMPORT_MODE_NEW_ONLY,
    IMPORT_MODE_SAFE,
    ImportFileChanged,
    ImportPreviewError,
    ImportPreviewNotFound,
    ImportRepeatedFile,
    apply_saved_import_preview,
    build_import_preview,
    save_import_preview,
)
from app.excel.source_key_backfill import apply_backfill_plan, build_backfill_plan
from app.services.statuses import NOTIFICATION_STATUS_PLANNED
from app.services.user_service import ROLE_ADMIN, ROLE_CHECKER


HEADERS = [
    "ID",
    "Дата",
    "Время выхода",
    "Проект",
    "Название ППР",
    "Активности",
    "Уведомлять о выходе",
    "Активно",
    "Исходная строка",
]


def workbook_bytes(rows: list[dict]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "ППР_для_бота"
    ws.append(HEADERS)
    for row in rows:
        ws.append(
            [
                row.get("id"),
                row.get("date"),
                row.get("start_time", time(8, 0)),
                row.get("project", "Project"),
                row.get("title", "PPR"),
                row.get("activities", "Work"),
                row.get("notify", True),
                row.get("active", True),
                row.get("source_row"),
            ]
        )
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


class ExcelImportPreviewTestCase(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(prefix="ppr-import-", suffix=".db", delete=False)
        handle.close()
        self.db_path = handle.name
        self.previous_outlook_enabled = os.environ.get("OUTLOOK_ENABLED")
        os.environ["OUTLOOK_ENABLED"] = "false"
        get_settings.cache_clear()
        self.engine = create_engine(f"sqlite:///{self.db_path}", connect_args={"check_same_thread": False}, future=True)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        Base.metadata.create_all(bind=self.engine)
        self.admin = SimpleNamespace(telegram_id="9001", username="admin", full_name="Admin", role=ROLE_ADMIN)
        self.tomorrow = date.today() + timedelta(days=1)

    def tearDown(self):
        if self.previous_outlook_enabled is None:
            os.environ.pop("OUTLOOK_ENABLED", None)
        else:
            os.environ["OUTLOOK_ENABLED"] = self.previous_outlook_enabled
        get_settings.cache_clear()
        self.engine.dispose()
        try:
            os.remove(self.db_path)
        except FileNotFoundError:
            pass

    def preview_and_apply(self, db, content: bytes, mode: str = IMPORT_MODE_SAFE, confirm_force: bool = False):
        preview = save_import_preview(db, content, "schedule.xlsx", mode, self.admin)
        return asyncio.run(apply_saved_import_preview(db, preview["preview_id"], content, mode, self.admin, confirm_force=confirm_force))

    def test_repeated_import_does_not_create_duplicates(self):
        content = workbook_bytes([{"id": "A-1", "date": self.tomorrow, "title": "First"}])
        with self.SessionLocal() as db:
            self.preview_and_apply(db, content)
            preview = save_import_preview(db, content, "schedule.xlsx", IMPORT_MODE_SAFE, self.admin)
            with self.assertRaises(ImportRepeatedFile):
                asyncio.run(apply_saved_import_preview(db, preview["preview_id"], content, IMPORT_MODE_SAFE, self.admin))
            self.assertEqual(db.query(PprEvent).count(), 1)
            self.assertEqual(db.query(PprNotification).count(), 1)

    def test_second_preview_of_same_file_is_unchanged_before_repeated_apply_is_rejected(self):
        content = workbook_bytes([{"id": "A-REPEAT", "date": self.tomorrow, "title": "Repeat"}])
        with self.SessionLocal() as db:
            self.preview_and_apply(db, content)
            preview = save_import_preview(db, content, "schedule.xlsx", IMPORT_MODE_SAFE, self.admin)
            detail = preview["details"][0]
            self.assertEqual(detail["action"], "unchanged")
            self.assertEqual(detail["match_method"], "source_key")
            self.assertTrue(preview["warnings"])
            with self.assertRaises(ImportRepeatedFile):
                asyncio.run(apply_saved_import_preview(db, preview["preview_id"], content, IMPORT_MODE_SAFE, self.admin))
            self.assertEqual(db.query(PprEvent).count(), 1)
            self.assertEqual(db.query(PprNotification).count(), 1)

    def test_safe_does_not_overwrite_manual_edit(self):
        first = workbook_bytes([{"id": "A-2", "date": self.tomorrow, "title": "Original"}])
        changed = workbook_bytes([{"id": "A-2", "date": self.tomorrow + timedelta(days=5), "title": "From Excel"}])
        with self.SessionLocal() as db:
            self.preview_and_apply(db, first)
            event = db.query(PprEvent).one()
            event.date = self.tomorrow + timedelta(days=2)
            event.title = "Manual"
            event.is_manually_edited = True
            db.commit()

            preview = build_import_preview(db, changed, "schedule.xlsx", IMPORT_MODE_SAFE)
            self.assertEqual(preview["summary"]["skipped_manual_events"], 1)
            self.preview_and_apply(db, changed)
            event = db.query(PprEvent).one()
            self.assertEqual(event.title, "Manual")
            self.assertEqual(event.date, self.tomorrow + timedelta(days=2))

    def test_force_overwrites_manual_edit(self):
        first = workbook_bytes([{"id": "A-3", "date": self.tomorrow, "title": "Original"}])
        changed = workbook_bytes([{"id": "A-3", "date": self.tomorrow + timedelta(days=4), "title": "Forced"}])
        with self.SessionLocal() as db:
            self.preview_and_apply(db, first)
            event = db.query(PprEvent).one()
            event.title = "Manual"
            event.is_manually_edited = True
            db.commit()

            self.preview_and_apply(db, changed, IMPORT_MODE_FORCE, confirm_force=True)
            event = db.query(PprEvent).one()
            self.assertEqual(event.title, "Forced")
            self.assertFalse(event.is_manually_edited)

    def test_new_only_does_not_change_existing_event(self):
        first = workbook_bytes([{"id": "A-4", "date": self.tomorrow, "title": "Original"}])
        changed = workbook_bytes([{"id": "A-4", "date": self.tomorrow + timedelta(days=4), "title": "Changed"}])
        with self.SessionLocal() as db:
            self.preview_and_apply(db, first)
            self.preview_and_apply(db, changed, IMPORT_MODE_NEW_ONLY)
            event = db.query(PprEvent).one()
            self.assertEqual(event.title, "Original")
            self.assertEqual(event.date, self.tomorrow)

    def test_duplicate_rows_are_reported(self):
        content = workbook_bytes([
            {"id": "DUP-1", "date": self.tomorrow, "title": "One"},
            {"id": "DUP-1", "date": self.tomorrow, "title": "Two"},
        ])
        with self.SessionLocal() as db:
            preview = build_import_preview(db, content, "schedule.xlsx")
            self.assertEqual(preview["summary"]["duplicate_rows"], 2)
            self.assertEqual(preview["summary"]["errors_count"], 2)

    def test_missing_date_does_not_create_notification(self):
        content = workbook_bytes([{"id": "A-5", "date": None, "title": "No date"}])
        with self.SessionLocal() as db:
            self.preview_and_apply(db, content)
            self.assertEqual(db.query(PprEvent).count(), 1)
            self.assertEqual(db.query(PprNotification).count(), 0)

    def test_date_change_updates_existing_notification(self):
        first = workbook_bytes([{"id": "A-6", "date": self.tomorrow, "title": "Original"}])
        changed_date = self.tomorrow + timedelta(days=3)
        changed = workbook_bytes([{"id": "A-6", "date": changed_date, "title": "Original"}])
        with self.SessionLocal() as db:
            self.preview_and_apply(db, first)
            initial_notification_id = db.query(PprNotification).one().id
            preview = build_import_preview(db, changed, "schedule.xlsx", IMPORT_MODE_SAFE)
            self.assertEqual(preview["details"][0]["action"], "update")
            self.assertEqual(preview["details"][0]["notification_action"], "update")
            self.preview_and_apply(db, changed)
            notifications = db.query(PprNotification).all()
            self.assertEqual(len(notifications), 1)
            self.assertEqual(notifications[0].id, initial_notification_id)
            self.assertEqual(notifications[0].scheduled_at.date(), changed_date)
            self.assertEqual(notifications[0].status, NOTIFICATION_STATUS_PLANNED)

    def test_explicit_id_matches_stable_source_key_on_changed_import(self):
        first = workbook_bytes([{"id": "STABLE-ID-1", "date": self.tomorrow, "title": "Stable ID"}])
        changed = workbook_bytes([{"id": "STABLE-ID-1", "date": self.tomorrow + timedelta(days=2), "title": "Stable ID"}])
        with self.SessionLocal() as db:
            self.preview_and_apply(db, first)
            preview = build_import_preview(db, changed, "schedule.xlsx", IMPORT_MODE_SAFE)
            detail = preview["details"][0]
            self.assertEqual(detail["action"], "update")
            self.assertEqual(detail["match_method"], "source_key")
            self.assertEqual(detail["source_key"], "id:STABLE-ID-1")
            self.preview_and_apply(db, changed)
            self.assertEqual(db.query(PprEvent).count(), 1)
            self.assertEqual(db.query(PprNotification).count(), 1)

    def test_duplicate_excel_rows_block_apply_without_partial_writes(self):
        content = workbook_bytes([
            {"id": "VALID-1", "date": self.tomorrow, "title": "Valid row"},
            {"id": "DUPLICATE-1", "date": self.tomorrow, "title": "Duplicate row"},
            {"id": "DUPLICATE-1", "date": self.tomorrow, "title": "Duplicate row"},
        ])
        with self.SessionLocal() as db:
            preview = save_import_preview(db, content, "schedule.xlsx", IMPORT_MODE_SAFE, self.admin)
            self.assertEqual(preview["summary"]["duplicate_rows"], 2)
            self.assertEqual(preview["summary"]["errors_count"], 2)
            self.assertTrue(all(item["action"] == "duplicate" for item in preview["details"] if item["external_id"] == "DUPLICATE-1"))

            with self.assertRaises(ImportPreviewError):
                asyncio.run(apply_saved_import_preview(db, preview["preview_id"], content, IMPORT_MODE_SAFE, self.admin))

            self.assertEqual(db.query(PprEvent).count(), 0)
            self.assertEqual(db.query(PprNotification).count(), 0)

    def test_ambiguous_fingerprint_blocks_apply_without_partial_writes(self):
        content = workbook_bytes([{"date": self.tomorrow, "title": "Ambiguous import"}])
        with self.SessionLocal() as db:
            db.add_all([
                PprEvent(external_id="LEGACY-A", title="Ambiguous import", project="Project", activities="Work", date=self.tomorrow, start_time=time(8, 0)),
                PprEvent(external_id="LEGACY-B", title="Ambiguous import", project="Project", activities="Work", date=self.tomorrow, start_time=time(8, 0)),
            ])
            db.commit()

            preview = save_import_preview(db, content, "schedule.xlsx", IMPORT_MODE_SAFE, self.admin)
            self.assertEqual(preview["details"][0]["action"], "ambiguous")
            self.assertEqual(preview["details"][0]["match_method"], "ambiguous")
            with self.assertRaises(ImportPreviewError):
                asyncio.run(apply_saved_import_preview(db, preview["preview_id"], content, IMPORT_MODE_SAFE, self.admin))

            self.assertEqual(db.query(PprEvent).count(), 2)
            self.assertEqual(db.query(PprNotification).count(), 0)

    def test_missing_from_excel_is_not_archived(self):
        first = workbook_bytes([
            {"id": "A-7", "date": self.tomorrow, "title": "Keep"},
            {"id": "A-8", "date": self.tomorrow, "title": "Missing later"},
        ])
        second = workbook_bytes([{"id": "A-7", "date": self.tomorrow, "title": "Keep"}])
        with self.SessionLocal() as db:
            self.preview_and_apply(db, first)
            preview = build_import_preview(db, second, "schedule.xlsx")
            self.assertEqual(preview["summary"]["events_missing_from_excel"], 1)
            self.preview_and_apply(db, second)
            missing = db.query(PprEvent).filter(PprEvent.external_id == "A-8").one()
            self.assertTrue(missing.is_active)

    def test_apply_rejects_changed_file_after_preview(self):
        first = workbook_bytes([{"id": "A-9", "date": self.tomorrow, "title": "First"}])
        second = workbook_bytes([{"id": "A-9", "date": self.tomorrow + timedelta(days=1), "title": "Second"}])
        with self.SessionLocal() as db:
            preview = save_import_preview(db, first, "schedule.xlsx", IMPORT_MODE_SAFE, self.admin)
            with self.assertRaises(ImportFileChanged):
                asyncio.run(apply_saved_import_preview(db, preview["preview_id"], second, IMPORT_MODE_SAFE, self.admin))

    def test_reordered_rows_without_ids_do_not_create_duplicates(self):
        first = workbook_bytes([
            {"date": self.tomorrow, "title": "No ID One"},
            {"date": self.tomorrow + timedelta(days=1), "title": "No ID Two"},
            {"date": self.tomorrow + timedelta(days=2), "title": "No ID Three"},
        ])
        reordered = workbook_bytes([
            {"date": self.tomorrow + timedelta(days=2), "title": "No ID Three"},
            {"date": self.tomorrow, "title": "No ID One"},
            {"date": self.tomorrow + timedelta(days=1), "title": "No ID Two"},
        ])
        with self.SessionLocal() as db:
            self.preview_and_apply(db, first)
            preview = build_import_preview(db, reordered, "schedule.xlsx", IMPORT_MODE_SAFE)
            self.assertEqual(preview["summary"]["new_events"], 0)
            self.assertEqual(preview["summary"]["key_by_method"]["fingerprint"], 3)
            self.assertEqual(preview["summary"]["matched_existing_by_source_key"], 3)
            self.assertTrue(all(item["match_method"] == "source_key" for item in preview["details"] if item["excel_row_number"]))
            self.preview_and_apply(db, reordered)
            self.assertEqual(db.query(PprEvent).count(), 3)

    def test_inserted_row_without_ids_creates_only_one_new_event(self):
        first = workbook_bytes([
            {"date": self.tomorrow, "title": "Stable One"},
            {"date": self.tomorrow + timedelta(days=1), "title": "Stable Two"},
        ])
        with_new_first_row = workbook_bytes([
            {"date": self.tomorrow + timedelta(days=5), "title": "Inserted New"},
            {"date": self.tomorrow, "title": "Stable One"},
            {"date": self.tomorrow + timedelta(days=1), "title": "Stable Two"},
        ])
        with self.SessionLocal() as db:
            self.preview_and_apply(db, first)
            preview = build_import_preview(db, with_new_first_row, "schedule.xlsx", IMPORT_MODE_SAFE)
            self.assertEqual(preview["summary"]["new_events"], 1)
            self.preview_and_apply(db, with_new_first_row)
            self.assertEqual(db.query(PprEvent).count(), 3)

    def test_date_change_without_id_updates_existing_event(self):
        first = workbook_bytes([{"date": self.tomorrow, "title": "Date Moves Without ID"}])
        moved = workbook_bytes([{"date": self.tomorrow + timedelta(days=9), "title": "Date Moves Without ID"}])
        with self.SessionLocal() as db:
            self.preview_and_apply(db, first)
            preview = build_import_preview(db, moved, "schedule.xlsx", IMPORT_MODE_SAFE)
            self.assertEqual(preview["summary"]["new_events"], 0)
            self.assertEqual(preview["summary"]["updated_events"], 1)
            self.preview_and_apply(db, moved)
            self.assertEqual(db.query(PprEvent).count(), 1)
            self.assertEqual(db.query(PprEvent).one().date, self.tomorrow + timedelta(days=9))

    def test_title_change_without_id_is_not_silently_matched(self):
        first = workbook_bytes([{"date": self.tomorrow, "title": "Original No ID Title"}])
        renamed = workbook_bytes([{"date": self.tomorrow, "title": "Renamed No ID Title"}])
        with self.SessionLocal() as db:
            self.preview_and_apply(db, first)
            preview = build_import_preview(db, renamed, "schedule.xlsx", IMPORT_MODE_SAFE)
            self.assertEqual(preview["summary"]["new_events"], 1)
            self.assertEqual(preview["summary"]["events_missing_from_excel"], 1)

    def test_legacy_external_id_without_source_key_is_not_genuinely_new(self):
        content = workbook_bytes([{"id": "LEGACY-1", "date": self.tomorrow, "title": "Legacy ID"}])
        with self.SessionLocal() as db:
            db.add(PprEvent(external_id="LEGACY-1", title="Legacy ID", date=self.tomorrow, start_time=time(8, 0)))
            db.commit()
            preview = build_import_preview(db, content, "schedule.xlsx", IMPORT_MODE_SAFE)
            self.assertEqual(preview["summary"]["genuinely_new"], 0)
            self.assertEqual(preview["summary"]["matched_existing_by_external_id"], 1)
            self.assertEqual(preview["details"][0]["key_method"], "id")
            self.assertEqual(preview["details"][0]["match_method"], "external_id")

    def test_legacy_row_keys_and_missing_source_keys_migrate_without_duplicates(self):
        content = workbook_bytes([
            {"date": self.tomorrow, "title": "Legacy Row Key"},
            {"date": self.tomorrow + timedelta(days=1), "title": "Legacy Missing Key"},
        ])
        with self.SessionLocal() as db:
            legacy_row_event = PprEvent(
                external_id="ROW-2",
                source_key="row:2",
                title="Legacy Row Key",
                project="Project",
                activities="Work",
                date=self.tomorrow,
                start_time=time(8, 0),
            )
            legacy_missing_key_event = PprEvent(
                external_id="ROW-99",
                source_key=None,
                title="Legacy Missing Key",
                project="Project",
                activities="Work",
                date=self.tomorrow + timedelta(days=1),
                start_time=time(8, 0),
            )
            db.add_all([legacy_row_event, legacy_missing_key_event])
            db.flush()
            db.add_all([
                PprNotification(
                    ppr_event_id=legacy_row_event.id,
                    type="start",
                    scheduled_at=datetime.combine(self.tomorrow, time(8, 0)),
                ),
                PprNotification(
                    ppr_event_id=legacy_missing_key_event.id,
                    type="start",
                    scheduled_at=datetime.combine(self.tomorrow + timedelta(days=1), time(8, 0)),
                ),
            ])
            db.commit()

            preview = build_import_preview(db, content, "schedule.xlsx", IMPORT_MODE_SAFE)
            details = [item for item in preview["details"] if item["excel_row_number"]]
            self.assertEqual([item["action"] for item in details], ["source_key_migration", "source_key_migration"])
            self.assertEqual([item["match_method"] for item in details], ["legacy_row", "fingerprint"])
            self.preview_and_apply(db, content)

            events = db.query(PprEvent).order_by(PprEvent.title).all()
            self.assertEqual(len(events), 2)
            self.assertEqual(db.query(PprNotification).count(), 2)
            self.assertTrue(all(event.source_key.startswith("fingerprint:") for event in events))
            self.assertTrue(all(event.external_id.startswith("AUTO-") for event in events))

    def test_manual_event_is_not_matched_or_overwritten_by_fingerprint_import(self):
        content = workbook_bytes([{"date": self.tomorrow, "title": "Manual Boundary"}])
        with self.SessionLocal() as db:
            manual = PprEvent(
                external_id="MANUAL-BOUNDARY",
                title="Manual Boundary",
                project="Project",
                activities="Work",
                date=self.tomorrow - timedelta(days=1),
                start_time=time(7, 0),
                comment="Do not overwrite",
                is_manually_edited=True,
            )
            db.add(manual)
            db.commit()

            preview = build_import_preview(db, content, "schedule.xlsx", IMPORT_MODE_SAFE)
            self.assertEqual(preview["details"][0]["action"], "create")
            self.assertEqual(preview["details"][0]["match_method"], "none")
            self.preview_and_apply(db, content)

            db.refresh(manual)
            self.assertEqual(manual.date, self.tomorrow - timedelta(days=1))
            self.assertEqual(manual.comment, "Do not overwrite")
            self.assertEqual(db.query(PprEvent).count(), 2)

    def test_backfill_by_external_id_assigns_id_source_key(self):
        content = workbook_bytes([{"id": "BACKFILL-1", "date": self.tomorrow, "title": "Backfill ID"}])
        with self.SessionLocal() as db:
            event = PprEvent(external_id="BACKFILL-1", title="Backfill ID", date=self.tomorrow, start_time=time(8, 0))
            db.add(event)
            db.commit()
            plan = build_backfill_plan(db, content)
            self.assertEqual(plan["summary"]["updates"], 1)
            result = apply_backfill_plan(db, plan)
            db.commit()
            self.assertEqual(result["updated"], 1)
            self.assertEqual(db.get(PprEvent, event.id).source_key, "id:BACKFILL-1")

    def test_repeated_backfill_is_idempotent(self):
        content = workbook_bytes([{"id": "BACKFILL-2", "date": self.tomorrow, "title": "Backfill Twice"}])
        with self.SessionLocal() as db:
            event = PprEvent(external_id="BACKFILL-2", title="Backfill Twice", date=self.tomorrow, start_time=time(8, 0))
            db.add(event)
            db.commit()
            plan = build_backfill_plan(db, content)
            apply_backfill_plan(db, plan)
            db.commit()
            second = build_backfill_plan(db, content)
            self.assertEqual(second["summary"]["updates"], 0)
            self.assertEqual(second["summary"]["noop"], 1)

    def test_backfill_unique_fingerprint_updates_source_key(self):
        content = workbook_bytes([{"date": self.tomorrow, "title": "Unique Fingerprint"}])
        with self.SessionLocal() as db:
            event = PprEvent(external_id="ROW-2", source_key=None, title="Unique Fingerprint", date=self.tomorrow, start_time=time(8, 0), project="Project", activities="Work")
            db.add(event)
            db.commit()
            plan = build_backfill_plan(db, content)
            self.assertEqual(plan["summary"]["matched_by_fingerprint"], 1)
            self.assertEqual(plan["summary"]["updates"], 1)
            apply_backfill_plan(db, plan)
            db.commit()
            self.assertTrue(db.get(PprEvent, event.id).source_key.startswith("fingerprint:"))

    def test_backfill_ambiguous_fingerprint_is_skipped(self):
        content = workbook_bytes([{"date": self.tomorrow, "title": "Ambiguous Fingerprint"}])
        with self.SessionLocal() as db:
            db.add(PprEvent(external_id="ROW-2", source_key=None, title="Ambiguous Fingerprint", date=self.tomorrow, start_time=time(8, 0), project="Project", activities="Work"))
            db.add(PprEvent(external_id="ROW-3", source_key=None, title="Ambiguous Fingerprint", date=self.tomorrow, start_time=time(8, 0), project="Project", activities="Work"))
            db.commit()
            plan = build_backfill_plan(db, content)
            self.assertEqual(plan["summary"]["ambiguous"], 1)
            self.assertEqual(plan["summary"]["updates"], 0)

    def test_manual_ppr_stays_without_source_key(self):
        content = workbook_bytes([{"date": self.tomorrow, "title": "Manual Looking"}])
        with self.SessionLocal() as db:
            db.add(PprEvent(external_id="MANUAL-1", source_key=None, title="Manual Looking", date=self.tomorrow, start_time=time(8, 0), project="Project", activities="Work"))
            db.commit()
            plan = build_backfill_plan(db, content)
            apply_backfill_plan(db, plan)
            db.commit()
            manual = db.query(PprEvent).filter(PprEvent.external_id == "MANUAL-1").one()
            self.assertIsNone(manual.source_key)

    def test_import_after_backfill_does_not_create_duplicate(self):
        content = workbook_bytes([{"id": "NO-DUP-1", "date": self.tomorrow, "title": "No Duplicate"}])
        with self.SessionLocal() as db:
            db.add(PprEvent(external_id="NO-DUP-1", source_key=None, title="No Duplicate", date=self.tomorrow, start_time=time(8, 0)))
            db.commit()
            plan = build_backfill_plan(db, content)
            apply_backfill_plan(db, plan)
            db.commit()
            preview = build_import_preview(db, content, "schedule.xlsx", IMPORT_MODE_SAFE)
            self.assertEqual(preview["summary"]["genuinely_new"], 0)
            self.assertEqual(db.query(PprEvent).count(), 1)


    def test_import_saves_found_outlook_link_with_mocked_graph(self):
        content = workbook_bytes([{"id": "OUTLOOK-FOUND", "date": self.tomorrow, "title": "Found"}])
        with self.SessionLocal() as db:
            with patch("app.services.outlook_graph.outlook_integration_configured", return_value=True), patch(
                "app.services.outlook_graph.OutlookGraphClient.find_calendar_link", new=AsyncMock(return_value="https://outlook.example/found")
            ) as find_link:
                self.preview_and_apply(db, content)
            event = db.query(PprEvent).one()
            self.assertEqual(event.outlook_link, "https://outlook.example/found")
            find_link.assert_awaited_once_with("Found", self.tomorrow)

    def test_import_keeps_ppr_when_outlook_event_is_not_found(self):
        content = workbook_bytes([{"id": "OUTLOOK-NOT-FOUND", "date": self.tomorrow, "title": "Missing"}])
        with self.SessionLocal() as db:
            with patch("app.services.outlook_graph.outlook_integration_configured", return_value=True), patch(
                "app.services.outlook_graph.OutlookGraphClient.find_calendar_link", new=AsyncMock(return_value=None)
            ) as find_link:
                result = self.preview_and_apply(db, content)
            event = db.query(PprEvent).one()
            self.assertEqual(result["applied"]["created"], 1)
            self.assertIsNone(event.outlook_link)
            find_link.assert_awaited_once_with("Missing", self.tomorrow)

    def test_import_skips_graph_when_outlook_is_not_configured(self):
        content = workbook_bytes([{"id": "OUTLOOK-DISABLED", "date": self.tomorrow, "title": "Disabled"}])
        with self.SessionLocal() as db:
            with patch("app.services.outlook_graph.outlook_integration_configured", return_value=False), patch(
                "app.services.outlook_graph.OutlookGraphClient"
            ) as graph_client:
                self.preview_and_apply(db, content)
            self.assertEqual(db.query(PprEvent).count(), 1)
            graph_client.assert_not_called()

    def test_outlook_error_does_not_rollback_import(self):
        content = workbook_bytes([{"id": "OUTLOOK-ERROR", "date": self.tomorrow, "title": "Failure"}])
        with self.SessionLocal() as db:
            with patch("app.services.outlook_graph.outlook_integration_configured", return_value=True), patch(
                "app.services.outlook_graph.OutlookGraphClient.find_calendar_link", new=AsyncMock(side_effect=RuntimeError("Graph failed"))
            ) as find_link:
                result = self.preview_and_apply(db, content)
            event = db.query(PprEvent).one()
            self.assertEqual(result["applied"]["created"], 1)
            self.assertEqual(event.external_id, "OUTLOOK-ERROR")
            self.assertIsNone(event.outlook_link)
            find_link.assert_awaited_once()

    def test_outlook_configuration_error_does_not_mark_import_failed(self):
        content = workbook_bytes([{"id": "OUTLOOK-CONFIG-ERROR", "date": self.tomorrow, "title": "Config failure"}])
        with self.SessionLocal() as db:
            with patch("app.services.outlook_graph.outlook_integration_configured", side_effect=RuntimeError("settings failed")):
                result = self.preview_and_apply(db, content)
            event = db.query(PprEvent).one()
            run = db.query(ImportRun).one()
            self.assertEqual(result["applied"]["created"], 1)
            self.assertEqual(event.external_id, "OUTLOOK-CONFIG-ERROR")
            self.assertEqual(run.status, "completed")

    def test_outlook_error_for_one_event_does_not_stop_other_syncs(self):
        content = workbook_bytes([
            {"id": "OUTLOOK-FIRST", "date": self.tomorrow, "title": "First"},
            {"id": "OUTLOOK-SECOND", "date": self.tomorrow, "title": "Second"},
        ])
        with self.SessionLocal() as db:
            with patch("app.services.outlook_graph.outlook_integration_configured", return_value=True), patch(
                "app.services.outlook_graph.OutlookGraphClient.find_calendar_link",
                new=AsyncMock(side_effect=[RuntimeError("first failed"), "https://outlook.example/second"]),
            ) as find_link:
                result = self.preview_and_apply(db, content)
            second = db.query(PprEvent).filter(PprEvent.external_id == "OUTLOOK-SECOND").one()
            self.assertEqual(result["applied"]["created"], 2)
            self.assertEqual(second.outlook_link, "https://outlook.example/second")
            self.assertEqual(find_link.await_count, 2)

    def test_import_does_not_overwrite_existing_outlook_link(self):
        first = workbook_bytes([{"id": "OUTLOOK-EXISTING", "date": self.tomorrow, "title": "Original"}])
        changed = workbook_bytes([{"id": "OUTLOOK-EXISTING", "date": self.tomorrow + timedelta(days=1), "title": "Changed"}])
        with self.SessionLocal() as db:
            self.preview_and_apply(db, first)
            event = db.query(PprEvent).one()
            event.outlook_link = "https://outlook.example/existing"
            db.commit()
            with patch("app.services.outlook_graph.outlook_integration_configured", return_value=True), patch(
                "app.services.outlook_graph.OutlookGraphClient.find_calendar_link", new=AsyncMock(return_value="https://outlook.example/replacement")
            ) as find_link:
                self.preview_and_apply(db, changed)
            db.refresh(event)
            self.assertEqual(event.outlook_link, "https://outlook.example/existing")
            find_link.assert_not_awaited()


class ExcelImportApiAuthTestCase(unittest.TestCase):
    def setUp(self):
        os.environ["DEV_COMMANDS_ENABLED"] = "true"
        get_settings.cache_clear()
        handle = tempfile.NamedTemporaryFile(prefix="ppr-import-api-", suffix=".db", delete=False)
        handle.close()
        self.db_path = handle.name
        self.previous_outlook_enabled = os.environ.get("OUTLOOK_ENABLED")
        os.environ["OUTLOOK_ENABLED"] = "false"
        get_settings.cache_clear()
        self.engine = create_engine(f"sqlite:///{self.db_path}", connect_args={"check_same_thread": False}, future=True)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        Base.metadata.create_all(bind=self.engine)
        with self.SessionLocal() as db:
            db.add(AppUser(telegram_id="100", username="admin", role=ROLE_ADMIN, is_active=True))
            db.add(AppUser(telegram_id="200", username="checker", role=ROLE_CHECKER, is_active=True))
            db.commit()

        self.content = workbook_bytes([{"id": "API-1", "date": date.today() + timedelta(days=1), "title": "API"}])

    def tearDown(self):
        if self.previous_outlook_enabled is None:
            os.environ.pop("OUTLOOK_ENABLED", None)
        else:
            os.environ["OUTLOOK_ENABLED"] = self.previous_outlook_enabled
        get_settings.cache_clear()
        self.engine.dispose()
        try:
            os.remove(self.db_path)
        except FileNotFoundError:
            pass

    def test_apply_without_preview_is_rejected(self):
        with self.SessionLocal() as db:
            with self.assertRaises(ImportPreviewNotFound):
                asyncio.run(apply_saved_import_preview(db, "missing-preview", self.content, IMPORT_MODE_SAFE, self.admin_user()))

    def test_checker_cannot_preview_import(self):
        dependency = require_roles(ROLE_ADMIN)
        checker = AppUser(telegram_id="200", username="checker", role=ROLE_CHECKER, is_active=True)
        with self.assertRaises(HTTPException) as ctx:
            dependency(checker)
        self.assertEqual(ctx.exception.status_code, 403)

    def admin_user(self):
        return SimpleNamespace(telegram_id="100", username="admin", full_name="Admin", role=ROLE_ADMIN)


if __name__ == "__main__":
    unittest.main()
