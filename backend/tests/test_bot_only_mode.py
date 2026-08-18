import asyncio
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from datetime import date, datetime, time
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.bot.keyboards import notification_keyboard
from app.bot import runner as bot_runner
from app.bot.runner import get_message_admin
from app.config import get_settings
from app.db.models import AppUser, AuditLog
from app.db.session import Base
from app.services.statuses import PPR_STATUS_IN_PROGRESS, PPR_STATUS_SCHEDULED
from app.services.telegram_sender import format_telegram_datetime, render_notification_details, render_notification_message
from app.services.user_service import (
    ROLE_ADMIN,
    ROLE_CHECKER,
    assign_checker_from_reply,
    create_user,
    get_or_sync_user,
    update_user,
)


class BotOnlyModeTestCase(unittest.TestCase):
    def setUp(self):
        self.original_env = {key: os.environ.get(key) for key in (
            "DEPLOYMENT_MODE",
            "WEBAPP_URL",
            "TELEGRAM_BOT_USERNAME",
            "TELEGRAM_MINIAPP_SHORT_NAME",
            "ADMIN_TELEGRAM_IDS",
            "DEFAULT_TIMEZONE",
        )}
        os.environ["ADMIN_TELEGRAM_IDS"] = "9001"
        os.environ["DEFAULT_TIMEZONE"] = "Europe/Moscow"
        get_settings.cache_clear()

        handle = tempfile.NamedTemporaryFile(prefix="ppr-bot-only-", suffix=".db", delete=False)
        handle.close()
        self.db_path = handle.name
        self.engine = create_engine(f"sqlite:///{self.db_path}", future=True)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        Base.metadata.create_all(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()
        try:
            os.remove(self.db_path)
        except FileNotFoundError:
            pass
        for key, value in self.original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()

    def set_mode(self, mode: str, webapp_url: str = ""):
        os.environ["DEPLOYMENT_MODE"] = mode
        os.environ["WEBAPP_URL"] = webapp_url
        os.environ["TELEGRAM_BOT_USERNAME"] = "pprsendbot"
        os.environ["TELEGRAM_MINIAPP_SHORT_NAME"] = ""
        get_settings.cache_clear()

    def test_bot_only_keyboard_has_details_without_card_deep_link(self):
        self.set_mode("bot_only")
        keyboard = notification_keyboard(8, PPR_STATUS_SCHEDULED)
        buttons = [button for row in keyboard.inline_keyboard for button in row]
        self.assertEqual([button.text for button in buttons], ["👀 Взять в работу", "ℹ️ Подробнее"])
        self.assertFalse(any(button.url for button in buttons))

    def test_full_keyboard_has_card_deep_link_and_details(self):
        self.set_mode("full", "https://miniapp.example.com")
        keyboard = notification_keyboard(15, PPR_STATUS_IN_PROGRESS)
        buttons = [button for row in keyboard.inline_keyboard for button in row]
        self.assertEqual([button.text for button in buttons], ["✅ Проверено", "ℹ️ Подробнее", "📋 Открыть карточку"])
        self.assertEqual(buttons[-1].url, "https://t.me/pprsendbot?startapp=notification_15")

    def test_details_renderer_works_without_miniapp(self):
        event = SimpleNamespace(
            title="Проверка ППР",
            project="Проект A",
            date=date(2026, 7, 13),
            start_time=time(8, 0),
            end_time=time(9, 30),
            ppr_status=PPR_STATUS_IN_PROGRESS,
            activities="Настройка; Проверка",
            responsible_setup="Иванов",
            responsible_report="Петров",
            comment="Без Mini App",
            outlook_url="https://outlook.example.com/event",
            outlook_link=None,
        )
        notification = SimpleNamespace(
            event=event,
            taken_by_name="@checker",
            taken_at=datetime(2026, 7, 13, 8, 5),
            checked_by_name=None,
            checked_at=None,
        )
        details = render_notification_details(notification)
        self.assertIn("Время завершения", details)
        self.assertIn("Без Mini App", details)
        self.assertIn("Outlook", details)
        self.assertIn("@checker", details)
        self.assertNotIn("<b>Проверено:</b>", details)

    def test_telegram_status_times_are_rendered_in_project_timezone(self):
        event = SimpleNamespace(
            title="Проверка ППР",
            project=None,
            date=None,
            start_time=None,
            end_time=None,
            ppr_status=PPR_STATUS_IN_PROGRESS,
            activities=None,
            responsible_setup=None,
            responsible_report=None,
            comment=None,
            outlook_url=None,
            outlook_link=None,
            source_link=None,
        )
        notification = SimpleNamespace(
            event=event,
            type="start",
            taken_by_name="@checker",
            taken_at=datetime(2026, 8, 18, 6, 30),
            checked_by_name="@admin",
            checked_at=datetime(2026, 8, 18, 6, 30),
        )

        details = render_notification_details(notification)
        message = render_notification_message(notification)

        self.assertIn("<b>Взято:</b> 18.08.2026 09:30", details)
        self.assertIn("<b>Проверено:</b> 18.08.2026 09:30", details)
        self.assertIn("<b>Взято:</b> 18.08.2026 09:30", message)
        self.assertIn("<b>Проверено:</b> 18.08.2026 09:30", message)

    def test_telegram_status_time_formatter_handles_missing_value(self):
        self.assertIsNone(format_telegram_datetime(None))

    def test_dryrun_with_no_due_notifications_is_safe_for_html_mode(self):
        class FakeMessage:
            def __init__(self):
                self.answers = []

            async def answer(self, text, **kwargs):
                self.answers.append((text, kwargs))

        class EmptySession:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

        message = FakeMessage()
        with patch.object(bot_runner, "SessionLocal", return_value=EmptySession()), patch.object(
            bot_runner, "get_due_notifications", return_value=[]
        ):
            asyncio.run(bot_runner.on_dryrun(message))

        self.assertEqual(len(message.answers), 1)
        text, _kwargs = message.answers[0]
        self.assertIn("scheduled_at ≤ now", text)
        self.assertNotIn("<", text)

    def test_configure_logging_is_repeatable_and_redacts_error_file(self):
        with tempfile.TemporaryDirectory(prefix="pprbot-logs-") as log_dir:
            with patch.object(
                bot_runner,
                "get_settings",
                return_value=SimpleNamespace(
                    telegram_bot_token="telegram-secret",
                    outlook_client_secret="outlook-secret",
                ),
            ):
                bot_runner.configure_logging(Path(log_dir))
                bot_runner.configure_logging(Path(log_dir))
                test_logger = bot_runner.logging.getLogger("test.logging")
                test_logger.info("info should stay on console")
                test_logger.error("failure telegram-secret outlook-secret")
                root_logger = bot_runner.logging.getLogger()
                for handler in list(root_logger.handlers):
                    handler.flush()

                error_log = Path(log_dir) / "errors.log"
                content = error_log.read_text(encoding="utf-8")
                self.assertIn("failure", content)
                self.assertNotIn("info should stay on console", content)
                self.assertNotIn("telegram-secret", content)
                self.assertNotIn("outlook-secret", content)
                for handler in list(root_logger.handlers):
                    handler.close()
                    root_logger.removeHandler(handler)

    def test_bot_only_start_script_does_not_include_frontend_or_cloudflare(self):
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(project_root, "scripts", "start-bot-only.ps1"), encoding="utf-8") as script:
            content = script.read().lower()
        self.assertIn(".runtime\\bot-only", content)
        self.assertIn("-m uvicorn app.main:app --host 127.0.0.1 --port 8000", content)
        self.assertNotIn("--reload", content)
        self.assertNotIn("npm.cmd", content)
        self.assertNotIn("run dev", content)
        self.assertNotIn("tunnel --url", content)

    def test_bot_only_env_validation_rejects_missing_token(self):
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        common_script = os.path.join(project_root, "scripts", "bot-only-common.ps1").replace("'", "''")
        command = (
            "$path=Join-Path $env:TEMP ('pprbot-env-'+[guid]::NewGuid().ToString('N')+'.env'); "
            "Set-Content -LiteralPath $path -Value @('DEPLOYMENT_MODE=bot_only','TELEGRAM_ENABLED=true',"
            "'TELEGRAM_CHAT_ID=-1001','DATABASE_URL=postgresql://example','ADMIN_TELEGRAM_IDS=1',"
            "'DEV_COMMANDS_ENABLED=false','NOTIFICATIONS_AUTO_SEND_ENABLED=false','PILOT_AUTO_SEND_ALLOWED=false','AUTO_SEND_ALLOW_MASS=false',"
            "'AUTO_SEND_MASS_LIMIT=10'); "
            f". '{common_script}'; "
            "try { Assert-PprBotOnlyProductionEnv $path | Out-Null; exit 2 } catch { exit 0 } finally { Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue }"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pilot_auto_send_validation_rules(self):
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        common_script = os.path.join(project_root, "scripts", "bot-only-common.ps1").replace("'", "''")

        def validate(auto_send, pilot_allowed, allow_mass="false", mass_limit="10"):
            values = [
                "DEPLOYMENT_MODE=bot_only",
                "TELEGRAM_ENABLED=true",
                "TELEGRAM_BOT_TOKEN=test-token",
                "TELEGRAM_CHAT_ID=-1001",
                "DATABASE_URL=postgresql://example",
                "ADMIN_TELEGRAM_IDS=1",
                "DEV_COMMANDS_ENABLED=false",
                f"NOTIFICATIONS_AUTO_SEND_ENABLED={auto_send}",
                f"PILOT_AUTO_SEND_ALLOWED={pilot_allowed}",
                f"AUTO_SEND_ALLOW_MASS={allow_mass}",
                f"AUTO_SEND_MASS_LIMIT={mass_limit}",
            ]
            ps_values = ",".join("'" + value.replace("'", "''") + "'" for value in values)
            command = (
                "$path=Join-Path $env:TEMP ('pprbot-pilot-'+[guid]::NewGuid().ToString('N')+'.env'); "
                f"Set-Content -LiteralPath $path -Value @({ps_values}); "
                f". '{common_script}'; "
                "try { Assert-PprBotOnlyProductionEnv $path | Out-Null; exit 0 } "
                "catch { exit 1 } finally { Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue }"
            )
            return subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=20,
            )

        self.assertEqual(validate("false", "false").returncode, 0)
        self.assertNotEqual(validate("true", "false").returncode, 0)
        allowed = validate("true", "true")
        self.assertEqual(allowed.returncode, 0)
        self.assertIn("Pilot auto-send is enabled", allowed.stdout + allowed.stderr)
        self.assertNotEqual(validate("true", "true", allow_mass="true").returncode, 0)
        self.assertNotEqual(validate("true", "true", mass_limit="11").returncode, 0)

    def test_powerShell_runner_chain_grouping(self):
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        common_script = os.path.join(project_root, "scripts", "bot-only-common.ps1").replace("'", "''")
        command = (
            f". '{common_script}'; "
            "$one=@([pscustomobject]@{ProcessId=10;ParentProcessId=1;Name='python.exe'},"
            "[pscustomobject]@{ProcessId=11;ParentProcessId=10;Name='python.exe'},"
            "[pscustomobject]@{ProcessId=12;ParentProcessId=11;Name='python.exe'},"
            "[pscustomobject]@{ProcessId=20;ParentProcessId=2;Name='python.exe'},"
            "[pscustomobject]@{ProcessId=21;ParentProcessId=20;Name='python.exe'}); "
            "$groups=@(Get-PprBotLogicalRunnerChains $one); "
            "if ($groups.Count -ne 2) { exit 1 }; "
            "$first=$groups | Where-Object { @($_.ProcessIds) -contains 10 }; "
            "if ($first.EffectiveProcessId -ne 12) { exit 2 }; "
            "$second=$groups | Where-Object { @($_.ProcessIds) -contains 20 }; "
            "if ($second.EffectiveProcessId -ne 21) { exit 3 }; exit 0"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_operational_scripts_include_backup_restore_and_tasks(self):
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

        def script(name: str) -> str:
            with open(os.path.join(project_root, "scripts", name), encoding="utf-8") as handle:
                return handle.read()

        self.assertIn("pg_dump", script("backup-db.ps1"))
        self.assertIn("Select-Object -Skip 14", script("backup-db.ps1"))
        self.assertIn("[switch]$Confirm", script("restore-db.ps1"))
        self.assertIn("pg_restore", script("restore-db.ps1"))
        self.assertIn("Register-ScheduledTask", script("install-bot-only-autostart.ps1"))
        self.assertIn("PPRBot Bot Only", script("install-bot-only-autostart.ps1"))
        self.assertIn("PPRBot Database Backup", script("install-db-backup-task.ps1"))

    def test_admin_can_manage_checker_and_audit_is_written(self):
        with self.SessionLocal() as db:
            admin = get_or_sync_user(db, "9001", "admin", "Primary Admin")
            checker = create_user(db, "1001", None, "Checker One", ROLE_CHECKER, True, actor=admin)
            self.assertEqual(checker.role, ROLE_CHECKER)
            self.assertTrue(checker.is_active)

            disabled = update_user(db, checker, is_active=False, actor=admin)
            self.assertFalse(disabled.is_active)
            actions = [entry.action for entry in db.query(AuditLog).order_by(AuditLog.id).all()]
            self.assertIn("user_created", actions)
            self.assertIn("user_disabled", actions)

    def test_checker_cannot_be_used_as_bot_admin_and_primary_admin_is_protected(self):
        with self.SessionLocal() as db:
            admin = get_or_sync_user(db, "9001", "admin", "Primary Admin")
            checker = create_user(db, "1001", None, "Checker One", ROLE_CHECKER, True, actor=admin)
            checker_message = SimpleNamespace(from_user=SimpleNamespace(id=1001, username=None, first_name="Checker", last_name=None))
            self.assertIsNone(get_message_admin(db, checker_message))

            primary = db.query(AppUser).filter(AppUser.telegram_id == "9001").one()
            with self.assertRaisesRegex(ValueError, "cannot be demoted"):
                update_user(db, primary, role=ROLE_CHECKER, actor=admin)
            with self.assertRaisesRegex(ValueError, "cannot be disabled"):
                update_user(db, primary, is_active=False, actor=admin)
            self.assertEqual(primary.role, ROLE_ADMIN)
            self.assertTrue(primary.is_active)

    def test_reply_assignment_requires_admin_and_reply(self):
        checker = SimpleNamespace(telegram_id="1001", username="checker", full_name="Checker", role=ROLE_CHECKER, is_active=True)
        with self.SessionLocal() as db:
            with self.assertRaises(PermissionError):
                assign_checker_from_reply(db, checker, "2001", "new_checker", "New Checker")

        class CommandMessage:
            text = "/user_add_reply checker"
            reply_to_message = None

            def __init__(self):
                self.answers = []
                self.from_user = SimpleNamespace(id=9001, username="admin", first_name="Admin", last_name=None, is_bot=False)

            async def answer(self, text, **kwargs):
                self.answers.append(text)

        import app.bot.runner as runner
        original_session_local = runner.SessionLocal
        runner.SessionLocal = self.SessionLocal
        try:
            message = CommandMessage()
            asyncio.run(runner.on_user_add_reply(message))
            self.assertIn("Ответьте этой командой", message.answers[0])
            with self.SessionLocal() as db:
                self.assertIsNone(db.query(AppUser).filter(AppUser.telegram_id == "2001").one_or_none())
        finally:
            runner.SessionLocal = original_session_local

    def test_admin_can_assign_new_reply_checker_and_audit_is_written(self):
        admin = SimpleNamespace(telegram_id="9001", username="admin", full_name="Admin", role=ROLE_ADMIN, is_active=True)
        with self.SessionLocal() as db:
            created = assign_checker_from_reply(db, admin, "2001", "new_checker", "New Checker")
            self.assertEqual(created.role, ROLE_CHECKER)
            self.assertTrue(created.is_active)
            self.assertEqual(created.username, "new_checker")
            self.assertTrue(any(item.action == "user_checker_assigned" for item in db.query(AuditLog).all()))

    def test_reply_assignment_updates_role_but_preserves_disabled_state(self):
        with self.SessionLocal() as db:
            admin = get_or_sync_user(db, "9001", "admin", "Primary Admin")
            existing = create_user(db, "2001", "old_name", "Old Name", ROLE_ADMIN, False, actor=admin)
            updated = assign_checker_from_reply(db, admin, "2001", "new_name", "New Name")
            self.assertEqual(updated.role, ROLE_CHECKER)
            self.assertFalse(updated.is_active)
            self.assertEqual(updated.username, "new_name")
            self.assertEqual(updated.full_name, "New Name")

    def test_reply_assignment_rejects_primary_admin_and_bots(self):
        admin = SimpleNamespace(telegram_id="9001", username="admin", full_name="Admin", role=ROLE_ADMIN, is_active=True)
        with self.SessionLocal() as db:
            with self.assertRaisesRegex(ValueError, "cannot be demoted"):
                assign_checker_from_reply(db, admin, "9001", "admin", "Admin")

        class CommandMessage:
            text = "/user_add_reply checker"

            def __init__(self):
                self.from_user = SimpleNamespace(id=9001, username="admin", first_name="Admin", last_name=None, is_bot=False)
                self.reply_to_message = SimpleNamespace(from_user=SimpleNamespace(id=3001, username="bot", first_name="Bot", last_name=None, is_bot=True))
                self.answers = []

            async def answer(self, text, **kwargs):
                self.answers.append(text)

        import app.bot.runner as runner
        original_session_local = runner.SessionLocal
        runner.SessionLocal = self.SessionLocal
        try:
            message = CommandMessage()
            asyncio.run(runner.on_user_add_reply(message))
            self.assertIn("Telegram-бота", message.answers[0])
            with self.SessionLocal() as db:
                self.assertIsNone(db.query(AppUser).filter(AppUser.telegram_id == "3001").one_or_none())
        finally:
            runner.SessionLocal = original_session_local


if __name__ == "__main__":
    unittest.main()
