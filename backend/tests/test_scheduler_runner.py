import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.scheduler import runner


class SchedulerSetupTests(unittest.TestCase):
    def _settings(self, auto_import: bool):
        return SimpleNamespace(
            default_timezone="Europe/Moscow",
            schedule_auto_import_enabled=auto_import,
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


if __name__ == "__main__":
    unittest.main()
