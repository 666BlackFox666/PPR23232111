import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from pydantic import ValidationError

from app.config import Settings
from app.excel.import_service import ImportPreviewError
from app.scheduler import runner


class SchedulerSetupTests(unittest.TestCase):
    def _settings(self, auto_import: bool, interval: int = 10, path: str = "schedule.xlsx"):
        return SimpleNamespace(
            default_timezone="Europe/Moscow",
            schedule_auto_import_enabled=auto_import,
            schedule_auto_import_interval_minutes=interval,
            schedule_xlsx_path=path,
            notifications_auto_send_enabled=False,
        )

    def test_auto_import_disabled_by_default(self):
        with patch.object(runner, "settings", self._settings(False)):
            scheduler = runner.setup_scheduler()

        self.assertIsNone(scheduler.get_job("import_schedule"))

    def test_auto_import_registered_when_enabled(self):
        with patch.object(runner, "settings", self._settings(True)):
            scheduler = runner.setup_scheduler()

        job = scheduler.get_job("import_schedule")
        self.assertIsNotNone(job)
        self.assertEqual(job.id, "import_schedule")
        self.assertEqual(job.trigger.interval.total_seconds(), 600)
        self.assertEqual(job.max_instances, 1)
        self.assertTrue(job.coalesce)

    def test_auto_import_uses_configured_interval(self):
        with patch.object(runner, "settings", self._settings(True, interval=17)):
            scheduler = runner.setup_scheduler()

        job = scheduler.get_job("import_schedule")
        self.assertEqual(job.trigger.interval.total_seconds(), 17 * 60)

    def test_interval_less_than_one_is_rejected_by_settings(self):
        with self.assertRaises(ValidationError):
            Settings(schedule_auto_import_interval_minutes=0)

    def test_import_job_imports_existing_file(self):
        with tempfile.NamedTemporaryFile(suffix=".xlsx") as schedule:
            fake_session = MagicMock()
            session_context = MagicMock()
            session_context.__enter__.return_value = fake_session
            session_context.__exit__.return_value = False
            with patch.object(runner, "settings", self._settings(True, path=schedule.name)), patch.object(
                runner, "SessionLocal", return_value=session_context
            ), patch.object(runner, "import_excel", new=AsyncMock()) as import_excel:
                asyncio.run(runner.import_schedule_job())

        import_excel.assert_awaited_once_with(fake_session, Path(schedule.name))

    def test_import_job_missing_file_logs_warning_and_does_not_open_database(self):
        missing_path = str(
            Path(tempfile.gettempdir()) / f"pprbot-missing-{uuid4().hex}.xlsx"
        )
        with patch.object(runner, "settings", self._settings(True, path=missing_path)), patch.object(
            runner, "SessionLocal"
        ) as session_local, patch.object(runner, "import_excel", new=AsyncMock()) as import_excel, self.assertLogs(
            runner.logger, level="WARNING"
        ) as logs:
            asyncio.run(runner.import_schedule_job())

        session_local.assert_not_called()
        import_excel.assert_not_awaited()
        self.assertIn("does not exist", "\n".join(logs.output))

    def test_import_job_logs_unexpected_error_without_raising(self):
        with tempfile.NamedTemporaryFile(suffix=".xlsx") as schedule:
            fake_session = MagicMock()
            session_context = MagicMock()
            session_context.__enter__.return_value = fake_session
            session_context.__exit__.return_value = False
            with patch.object(runner, "settings", self._settings(True, path=schedule.name)), patch.object(
                runner, "SessionLocal", return_value=session_context
            ), patch.object(
                runner, "import_excel", new=AsyncMock(side_effect=RuntimeError("broken import"))
            ), self.assertLogs(runner.logger, level="ERROR") as logs:
                asyncio.run(runner.import_schedule_job())

        self.assertIn("iteration failed", "\n".join(logs.output))

    def test_import_job_file_disappearing_after_check_is_warning_only(self):
        with tempfile.NamedTemporaryFile(suffix=".xlsx") as schedule:
            fake_session = MagicMock()
            session_context = MagicMock()
            session_context.__enter__.return_value = fake_session
            session_context.__exit__.return_value = False
            with patch.object(runner, "settings", self._settings(True, path=schedule.name)), patch.object(
                runner, "SessionLocal", return_value=session_context
            ), patch.object(
                runner,
                "import_excel",
                new=AsyncMock(side_effect=FileNotFoundError(schedule.name)),
            ), self.assertLogs(runner.logger, level="WARNING") as logs:
                asyncio.run(runner.import_schedule_job())

        self.assertIn("disappeared", "\n".join(logs.output))

    def test_import_job_unreadable_file_is_warning_only(self):
        with tempfile.NamedTemporaryFile(suffix=".xlsx") as schedule:
            fake_session = MagicMock()
            session_context = MagicMock()
            session_context.__enter__.return_value = fake_session
            session_context.__exit__.return_value = False
            with patch.object(runner, "settings", self._settings(True, path=schedule.name)), patch.object(
                runner, "SessionLocal", return_value=session_context
            ), patch.object(
                runner,
                "import_excel",
                new=AsyncMock(side_effect=PermissionError("temporarily unavailable")),
            ), self.assertLogs(runner.logger, level="WARNING") as logs:
                asyncio.run(runner.import_schedule_job())

        self.assertIn("cannot be read", "\n".join(logs.output))

    def test_import_job_expected_import_error_is_warning_without_traceback(self):
        with tempfile.NamedTemporaryFile(suffix=".xlsx") as schedule:
            fake_session = MagicMock()
            session_context = MagicMock()
            session_context.__enter__.return_value = fake_session
            session_context.__exit__.return_value = False
            with patch.object(runner, "settings", self._settings(True, path=schedule.name)), patch.object(
                runner, "SessionLocal", return_value=session_context
            ), patch.object(
                runner,
                "import_excel",
                new=AsyncMock(side_effect=ImportPreviewError("invalid workbook")),
            ), self.assertLogs(runner.logger, level="WARNING") as logs:
                asyncio.run(runner.import_schedule_job())

        self.assertIn("validation or apply failed", "\n".join(logs.output))
        self.assertNotIn("Traceback", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
