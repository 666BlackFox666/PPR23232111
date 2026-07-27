import asyncio
import os
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.auth import require_roles
from app.bot import runner as bot_runner
from app.config import get_settings
from app.db.models import AppUser, AuditLog, PprEvent, PprNotification
from app.db.session import Base
from app.services.ppr_service import get_notification, requeue_notification, take_notification
from app.services.statuses import (
    NOTIFICATION_STATUS_DELIVERY_UNKNOWN,
    NOTIFICATION_STATUS_FAILED,
    NOTIFICATION_STATUS_PLANNED,
    NOTIFICATION_STATUS_PROCESSING,
    NOTIFICATION_STATUS_SENT,
    NOTIFICATION_STATUS_SKIPPED,
    PPR_STATUS_ARCHIVED,
    PPR_STATUS_IN_PROGRESS,
    PPR_STATUS_SCHEDULED,
)
from app.services.telegram_sender import (
    claim_notification_for_processing,
    mark_scheduler_poll_finished,
    mark_scheduler_poll_started,
    mark_scheduler_started,
    mark_unknown_notification_sent,
    list_notification_history,
    recover_stale_processing_notifications,
    retry_failed_notification,
    retry_unknown_notification,
    scheduler_status,
    send_due_notifications,
    send_notification,
)
from app.bot.runner import render_history, split_telegram_text
from app.services.user_service import ROLE_ADMIN, ROLE_CHECKER


class FakeMessage:
    def __init__(self, message_id=500):
        self.message_id = message_id


class FakeBot:
    def __init__(self, fail: Exception | None = None, message_id: int = 500):
        self.fail = fail
        self.message_id = message_id
        self.sent = []
        self.edits = []

    async def send_message(self, **kwargs):
        if self.fail:
            raise self.fail
        self.sent.append(kwargs)
        return FakeMessage(self.message_id)

    async def edit_message_text(self, **kwargs):
        if self.fail:
            raise self.fail
        self.edits.append(kwargs)
        return True


class FakeDetailsMessage:
    def __init__(self, *, old_message_id=100, chat_id=-1001, new_message_id=200, send_error=None, delete_error=None):
        self.message_id = old_message_id
        self.chat = SimpleNamespace(id=chat_id)
        self.new_message_id = new_message_id
        self.send_error = send_error
        self.delete_error = delete_error
        self.sent = []
        self.delete_calls = 0

    async def answer(self, text, **kwargs):
        if self.send_error:
            raise self.send_error
        self.sent.append((text, kwargs))
        return SimpleNamespace(message_id=self.new_message_id, chat=SimpleNamespace(id=self.chat.id))

    async def delete(self):
        self.delete_calls += 1
        if self.delete_error:
            raise self.delete_error


class FakeDetailsCallback:
    def __init__(self, notification_id: int, message: FakeDetailsMessage):
        self.data = f"details:{notification_id}"
        self.message = message
        self.from_user = SimpleNamespace(id=9001, username="admin", first_name="Admin", last_name=None)
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


class NotificationDeliveryTestCase(unittest.TestCase):
    def setUp(self):
        os.environ["TELEGRAM_ENABLED"] = "true"
        os.environ["TELEGRAM_BOT_TOKEN"] = "123:test"
        os.environ["TELEGRAM_CHAT_ID"] = "-1001"
        os.environ["NOTIFICATIONS_AUTO_SEND_ENABLED"] = "true"
        os.environ["AUTO_SEND_MASS_LIMIT"] = "10"
        os.environ["AUTO_SEND_ALLOW_MASS"] = "false"
        os.environ["AUTO_SEND_MAX_ATTEMPTS"] = "2"
        os.environ["AUTO_SEND_RETRY_DELAY_SECONDS"] = "1"
        os.environ["AUTO_SEND_MAX_LATE_MINUTES"] = "60"
        os.environ["PROCESSING_STALE_AFTER_SECONDS"] = "300"
        get_settings.cache_clear()

        handle = tempfile.NamedTemporaryFile(prefix="ppr-delivery-", suffix=".db", delete=False)
        handle.close()
        self.db_path = handle.name
        self.engine = create_engine(f"sqlite:///{self.db_path}", connect_args={"check_same_thread": False}, future=True)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        Base.metadata.create_all(bind=self.engine)

    def tearDown(self):
        get_settings.cache_clear()
        self.engine.dispose()
        try:
            os.remove(self.db_path)
        except FileNotFoundError:
            pass

    def create_notification(
        self,
        *,
        scheduled_at: datetime | None = None,
        status=NOTIFICATION_STATUS_PLANNED,
        auto_send_enabled=True,
        active=True,
        event_status=PPR_STATUS_SCHEDULED,
    ) -> int:
        scheduled_at = scheduled_at or (datetime.now() - timedelta(minutes=5))
        with self.SessionLocal() as db:
            event = PprEvent(
                external_id=f"DELIVERY-{uuid4()}",
                source_key=f"id:{uuid4()}",
                date=scheduled_at.date(),
                start_time=scheduled_at.time().replace(microsecond=0),
                title="Delivery PPR",
                project="Delivery",
                notify_start=True,
                is_active=active,
                ppr_status=event_status,
            )
            db.add(event)
            db.flush()
            notif = PprNotification(
                ppr_event_id=event.id,
                type="start",
                scheduled_at=scheduled_at,
                status=status,
                auto_send_enabled=auto_send_enabled,
            )
            db.add(notif)
            db.commit()
            return notif.id

    def run_details_callback(self, notification_id: int, message: FakeDetailsMessage):
        callback = FakeDetailsCallback(notification_id, message)
        app_user = SimpleNamespace(is_active=True, role=ROLE_ADMIN, telegram_id="9001")
        with patch.object(bot_runner, "SessionLocal", self.SessionLocal), patch.object(
            bot_runner, "get_or_sync_user", return_value=app_user
        ):
            asyncio.run(bot_runner.on_details(callback))
        return callback

    def test_details_creates_new_message_with_keyboard(self):
        notification_id = self.create_notification()
        message = FakeDetailsMessage()

        self.run_details_callback(notification_id, message)

        self.assertEqual(len(message.sent), 1)
        _text, kwargs = message.sent[0]
        self.assertTrue(kwargs["disable_web_page_preview"])
        button_texts = [button.text for row in kwargs["reply_markup"].inline_keyboard for button in row]
        self.assertIn("👀 Взять в работу", button_texts)

    def test_details_saves_new_message_ids_before_replacing_original(self):
        notification_id = self.create_notification()
        message = FakeDetailsMessage(chat_id=-100777, new_message_id=201)

        self.run_details_callback(notification_id, message)

        with self.SessionLocal() as db:
            notif = get_notification(db, notification_id)
            self.assertEqual(notif.telegram_chat_id, "-100777")
            self.assertEqual(notif.telegram_message_id, "201")

    def test_details_deletes_original_message_after_successful_send(self):
        notification_id = self.create_notification()
        message = FakeDetailsMessage()

        self.run_details_callback(notification_id, message)

        self.assertEqual(message.delete_calls, 1)

    def test_details_does_not_delete_original_when_new_message_fails(self):
        notification_id = self.create_notification()
        message = FakeDetailsMessage(send_error=OSError("send failed"))

        callback = self.run_details_callback(notification_id, message)

        self.assertEqual(message.sent, [])
        self.assertEqual(message.delete_calls, 0)
        self.assertEqual(callback.answers[-1], ("Не удалось открыть подробности", {"show_alert": True}))

    def test_details_keeps_new_message_when_original_delete_fails(self):
        notification_id = self.create_notification()
        message = FakeDetailsMessage(new_message_id=202, delete_error=OSError("delete failed"))

        callback = self.run_details_callback(notification_id, message)

        self.assertEqual(len(message.sent), 1)
        self.assertEqual(message.delete_calls, 1)
        self.assertEqual(callback.answers[-1], (None, {}))
        with self.SessionLocal() as db:
            notif = get_notification(db, notification_id)
            self.assertEqual(notif.telegram_message_id, "202")

    def test_details_keyboard_uses_current_ppr_status(self):
        notification_id = self.create_notification(event_status=PPR_STATUS_IN_PROGRESS)
        message = FakeDetailsMessage()

        self.run_details_callback(notification_id, message)

        _text, kwargs = message.sent[0]
        button_texts = [button.text for row in kwargs["reply_markup"].inline_keyboard for button in row]
        self.assertIn("✅ Проверено", button_texts)
        self.assertNotIn("👀 Взять в работу", button_texts)

    def test_due_notification_is_sent_once_and_message_ids_saved(self):
        notification_id = self.create_notification()
        bot = FakeBot(message_id=777)
        with self.SessionLocal() as db:
            sent = asyncio.run(send_due_notifications(db, bot=bot))
            self.assertEqual(sent, 1)
            self.assertEqual(len(bot.sent), 1)
            notif = get_notification(db, notification_id)
            self.assertEqual(notif.status, NOTIFICATION_STATUS_SENT)
            self.assertEqual(notif.telegram_chat_id, "-1001")
            self.assertEqual(notif.telegram_message_id, "777")
            self.assertIsNotNone(notif.sent_at)

            sent_again = asyncio.run(send_due_notifications(db, bot=bot))
            self.assertEqual(sent_again, 0)
            self.assertEqual(len(bot.sent), 1)

    def test_two_workers_cannot_claim_same_notification(self):
        notification_id = self.create_notification()
        db1 = self.SessionLocal()
        db2 = self.SessionLocal()
        try:
            first = claim_notification_for_processing(db1, notification_id, "worker-1")
            second = claim_notification_for_processing(db2, notification_id, "worker-2")
            self.assertIsNotNone(first)
            self.assertIsNone(second)
            fresh = get_notification(db1, notification_id)
            self.assertEqual(fresh.status, NOTIFICATION_STATUS_PROCESSING)
            self.assertEqual(fresh.processing_by, "worker-1")
        finally:
            db1.close()
            db2.close()

    def test_failed_notification_retries_until_limit(self):
        notification_id = self.create_notification()
        bot = FakeBot(fail=OSError("network down"))
        with self.SessionLocal() as db:
            sent = asyncio.run(send_due_notifications(db, bot=bot))
            self.assertEqual(sent, 0)
            notif = get_notification(db, notification_id)
            self.assertEqual(notif.status, NOTIFICATION_STATUS_PLANNED)
            self.assertEqual(notif.attempt_count, 1)
            notif.last_attempt_at = datetime.utcnow() - timedelta(seconds=5)
            db.commit()

            sent = asyncio.run(send_due_notifications(db, bot=bot))
            self.assertEqual(sent, 0)
            notif = get_notification(db, notification_id)
            self.assertEqual(notif.status, NOTIFICATION_STATUS_FAILED)
            self.assertEqual(notif.attempt_count, 2)
            self.assertIn("network down", notif.last_error)

    def test_too_late_notification_is_skipped(self):
        notification_id = self.create_notification(scheduled_at=datetime.now() - timedelta(hours=3))
        bot = FakeBot()
        with self.SessionLocal() as db:
            sent = asyncio.run(send_due_notifications(db, bot=bot))
            self.assertEqual(sent, 0)
            notif = get_notification(db, notification_id)
            self.assertEqual(notif.status, NOTIFICATION_STATUS_SKIPPED)
            self.assertEqual(notif.last_error, "too_late")
            self.assertEqual(len(bot.sent), 0)

    def test_auto_send_disabled_notification_is_not_sent(self):
        notification_id = self.create_notification(auto_send_enabled=False)
        bot = FakeBot()
        with self.SessionLocal() as db:
            sent = asyncio.run(send_due_notifications(db, bot=bot))
            self.assertEqual(sent, 0)
            notif = get_notification(db, notification_id)
            self.assertEqual(notif.status, NOTIFICATION_STATUS_PLANNED)
            self.assertEqual(len(bot.sent), 0)

    def test_archived_ppr_is_not_sent(self):
        notification_id = self.create_notification(active=False, event_status=PPR_STATUS_ARCHIVED)
        bot = FakeBot()
        with self.SessionLocal() as db:
            sent = asyncio.run(send_due_notifications(db, bot=bot))
            self.assertEqual(sent, 0)
            notif = get_notification(db, notification_id)
            self.assertEqual(notif.status, NOTIFICATION_STATUS_PLANNED)
            self.assertEqual(len(bot.sent), 0)

    def test_mass_send_protection_blocks_all(self):
        os.environ["AUTO_SEND_MASS_LIMIT"] = "1"
        get_settings.cache_clear()
        self.create_notification()
        self.create_notification()
        bot = FakeBot()
        with self.SessionLocal() as db:
            sent = asyncio.run(send_due_notifications(db, bot=bot))
            self.assertEqual(sent, 0)
            self.assertEqual(len(bot.sent), 0)
            self.assertEqual(db.query(PprNotification).filter(PprNotification.status == NOTIFICATION_STATUS_PLANNED).count(), 2)

    def test_manual_sendtest_can_send_auto_send_disabled(self):
        notification_id = self.create_notification(auto_send_enabled=False)
        bot = FakeBot()
        with self.SessionLocal() as db:
            notif = get_notification(db, notification_id)
            sent = asyncio.run(send_notification(db, notif, bot=bot, chat_id="-1001"))
            self.assertTrue(sent)
            self.assertEqual(len(bot.sent), 1)
            fresh = get_notification(db, notification_id)
            self.assertEqual(fresh.status, NOTIFICATION_STATUS_SENT)

    def test_edit_message_error_does_not_rollback_take(self):
        notification_id = self.create_notification(status=NOTIFICATION_STATUS_SENT)
        original_session_local = bot_runner.SessionLocal
        bot_runner.SessionLocal = self.SessionLocal
        try:
            with self.SessionLocal() as db:
                notif = get_notification(db, notification_id)
                notif.telegram_chat_id = "-1001"
                notif.telegram_message_id = "888"
                db.commit()
                ok, _, taken = take_notification(db, notification_id, "200", "@checker")
                self.assertTrue(ok)

            asyncio.run(bot_runner.edit_saved_notification_message(FakeBot(fail=RuntimeError("edit failed")), taken))

            with self.SessionLocal() as db:
                fresh = get_notification(db, notification_id)
                self.assertEqual(fresh.event.ppr_status, "in_progress")
                self.assertIn("edit failed", fresh.telegram_edit_last_error)
        finally:
            bot_runner.SessionLocal = original_session_local

    def test_checker_cannot_retry_but_admin_can(self):
        dependency = require_roles(ROLE_ADMIN)
        checker = AppUser(telegram_id="200", username="checker", role=ROLE_CHECKER, is_active=True)
        with self.assertRaises(HTTPException) as ctx:
            dependency(checker)
        self.assertEqual(ctx.exception.status_code, 403)

        notification_id = self.create_notification(status=NOTIFICATION_STATUS_FAILED)
        with self.SessionLocal() as db:
            retried = retry_failed_notification(db, notification_id)
            self.assertEqual(retried.status, NOTIFICATION_STATUS_PLANNED)

    def test_stale_claimed_processing_returns_to_planned(self):
        notification_id = self.create_notification()
        with self.SessionLocal() as db:
            notif = claim_notification_for_processing(db, notification_id, "worker-1")
            notif.processing_started_at = datetime.utcnow() - timedelta(minutes=10)
            notif.processing_phase = "claimed"
            db.commit()
            result = recover_stale_processing_notifications(db)
            self.assertEqual(result["recovered_claimed"], 1)
            fresh = get_notification(db, notification_id)
            self.assertEqual(fresh.status, NOTIFICATION_STATUS_PLANNED)
            self.assertIsNone(fresh.processing_by)

    def test_stale_sending_processing_becomes_delivery_unknown(self):
        notification_id = self.create_notification()
        with self.SessionLocal() as db:
            notif = claim_notification_for_processing(db, notification_id, "worker-1")
            notif.processing_started_at = datetime.utcnow() - timedelta(minutes=10)
            notif.processing_phase = "sending"
            db.commit()
            result = recover_stale_processing_notifications(db)
            self.assertEqual(result["delivery_unknown"], 1)
            fresh = get_notification(db, notification_id)
            self.assertEqual(fresh.status, NOTIFICATION_STATUS_DELIVERY_UNKNOWN)
            self.assertEqual(fresh.last_error, "Worker stopped while delivery result was unknown")

    def test_fresh_processing_is_not_touched(self):
        notification_id = self.create_notification()
        with self.SessionLocal() as db:
            claim_notification_for_processing(db, notification_id, "worker-1")
            result = recover_stale_processing_notifications(db)
            self.assertEqual(result["delivery_unknown"], 0)
            self.assertEqual(result["recovered_claimed"], 0)
            fresh = get_notification(db, notification_id)
            self.assertEqual(fresh.status, NOTIFICATION_STATUS_PROCESSING)

    def test_delivery_unknown_is_not_auto_sent(self):
        notification_id = self.create_notification(status=NOTIFICATION_STATUS_DELIVERY_UNKNOWN)
        bot = FakeBot()
        with self.SessionLocal() as db:
            sent = asyncio.run(send_due_notifications(db, bot=bot))
            self.assertEqual(sent, 0)
            self.assertEqual(len(bot.sent), 0)
            self.assertEqual(get_notification(db, notification_id).status, NOTIFICATION_STATUS_DELIVERY_UNKNOWN)

    def test_admin_mark_sent_is_idempotent_and_writes_audit(self):
        admin = SimpleNamespace(telegram_id="9001", username="admin", full_name="Admin")
        notification_id = self.create_notification(status=NOTIFICATION_STATUS_DELIVERY_UNKNOWN)
        with self.SessionLocal() as db:
            marked = mark_unknown_notification_sent(db, notification_id, admin, "message exists in Telegram")
            self.assertEqual(marked.status, NOTIFICATION_STATUS_SENT)
            marked_again = mark_unknown_notification_sent(db, notification_id, admin, "second call")
            self.assertEqual(marked_again.status, NOTIFICATION_STATUS_SENT)
            audits = db.query(PprNotification).filter(PprNotification.id == notification_id).one().audit_logs
            self.assertEqual(sum(1 for item in audits if item.action == "delivery_unknown_mark_sent"), 1)

    def test_retry_unknown_requires_confirm_and_writes_audit(self):
        admin = SimpleNamespace(telegram_id="9001", username="admin", full_name="Admin")
        notification_id = self.create_notification(status=NOTIFICATION_STATUS_DELIVERY_UNKNOWN)
        with self.SessionLocal() as db:
            with self.assertRaises(ValueError):
                retry_unknown_notification(db, notification_id, admin, confirm=False)
            retried = retry_unknown_notification(db, notification_id, admin, confirm=True)
            self.assertEqual(retried.status, NOTIFICATION_STATUS_PLANNED)
            audits = db.query(PprNotification).filter(PprNotification.id == notification_id).one().audit_logs
            self.assertEqual(sum(1 for item in audits if item.action == "delivery_unknown_retry"), 1)

    def test_scheduler_status_counts(self):
        self.create_notification()
        self.create_notification(status=NOTIFICATION_STATUS_FAILED)
        self.create_notification(status=NOTIFICATION_STATUS_DELIVERY_UNKNOWN)
        processing_id = self.create_notification(status=NOTIFICATION_STATUS_PROCESSING)
        with self.SessionLocal() as db:
            processing = get_notification(db, processing_id)
            processing.processing_started_at = datetime.utcnow() - timedelta(minutes=10)
            processing.processing_by = "worker-old"
            db.commit()
            mark_scheduler_started(db, "worker-1")
            mark_scheduler_poll_started(db, "worker-1")
            mark_scheduler_poll_finished(db, "worker-1")
            status = scheduler_status(db)
            self.assertTrue(status["scheduler_running"])
            self.assertEqual(status["worker_id"], "worker-1")
            self.assertGreaterEqual(status["due_count"], 1)
            self.assertEqual(status["processing_count"], 1)
            self.assertEqual(status["stale_processing_count"], 1)
            self.assertEqual(status["failed_count"], 1)
            self.assertEqual(status["delivery_unknown_count"], 1)

    def test_history_checker_is_denied(self):
        class CommandMessage:
            text = "/history"

            async def answer(self, text, **kwargs):
                self.response = text

        original_session_local = bot_runner.SessionLocal
        original_get_admin = bot_runner.get_message_admin
        bot_runner.SessionLocal = self.SessionLocal
        bot_runner.get_message_admin = lambda db, message: None
        try:
            message = CommandMessage()
            asyncio.run(bot_runner.on_history(message))
            self.assertEqual(message.response, "Нет доступа")
        finally:
            bot_runner.SessionLocal = original_session_local
            bot_runner.get_message_admin = original_get_admin

    def test_history_admin_sees_latest_records_and_order(self):
        first = self.create_notification(scheduled_at=datetime(2026, 7, 14, 10, 0))
        second = self.create_notification(scheduled_at=datetime(2026, 7, 14, 11, 0))
        with self.SessionLocal() as db:
            get_notification(db, first).event.title = "Older"
            get_notification(db, second).event.title = "Newer"
            db.commit()
            items = list_notification_history(db, limit=10)
            self.assertEqual([item.id for item in items[:2]], [second, first])

        class CommandMessage:
            text = "/history 10"

            def __init__(self):
                self.responses = []

            async def answer(self, text, **kwargs):
                self.responses.append(text)

        admin = SimpleNamespace(telegram_id="9001", username="admin", full_name="Admin", role=ROLE_ADMIN, is_active=True)
        original_session_local = bot_runner.SessionLocal
        original_get_admin = bot_runner.get_message_admin
        bot_runner.SessionLocal = self.SessionLocal
        bot_runner.get_message_admin = lambda db, message: admin
        try:
            message = CommandMessage()
            asyncio.run(bot_runner.on_history(message))
            joined = "\n".join(message.responses)
            self.assertIn("Newer", joined)
            self.assertLess(joined.index("Newer"), joined.index("Older"))
        finally:
            bot_runner.SessionLocal = original_session_local
            bot_runner.get_message_admin = original_get_admin

    def test_history_filters_notification_and_ppr_status(self):
        sent_id = self.create_notification(status=NOTIFICATION_STATUS_SENT)
        failed_id = self.create_notification(status=NOTIFICATION_STATUS_FAILED)
        in_progress_id = self.create_notification(event_status="in_progress")
        with self.SessionLocal() as db:
            self.assertEqual([item.id for item in list_notification_history(db, status=NOTIFICATION_STATUS_FAILED)], [failed_id])
            self.assertEqual([item.id for item in list_notification_history(db, status="in_progress")], [in_progress_id])
            self.assertNotIn(sent_id, [item.id for item in list_notification_history(db, status="in_progress")])

    def test_history_limit_is_capped_and_tie_order_uses_id(self):
        scheduled_at = datetime(2026, 7, 14, 12, 0)
        ids = [self.create_notification(scheduled_at=scheduled_at) for _ in range(3)]
        with self.SessionLocal() as db:
            items = list_notification_history(db, limit=999)
            self.assertLessEqual(len(items), 50)
            matching = [item.id for item in items if item.id in ids]
            self.assertEqual(matching, sorted(ids, reverse=True))

    def test_history_long_response_is_split_and_html_safe(self):
        long_title = "<PPR & " + ("x" * 4200) + ">"
        notification = SimpleNamespace(
            id=1,
            event=SimpleNamespace(title=long_title, project="Project & <A>", ppr_status="scheduled"),
            scheduled_at=datetime(2026, 7, 14, 12, 0),
            status=NOTIFICATION_STATUS_SENT,
            sent_at=None,
            auto_send_enabled=True,
        )
        text = render_history([notification], None, 10)
        chunks = split_telegram_text(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 4000 for chunk in chunks))
        self.assertNotIn("<PPR", text)
        self.assertIn("&lt;PPR", text)

    def test_checker_cannot_requeue_notification(self):
        notification_id = self.create_notification(status=NOTIFICATION_STATUS_FAILED)
        checker = SimpleNamespace(telegram_id="200", username="checker", full_name="Checker", role=ROLE_CHECKER, is_active=True)
        with self.SessionLocal() as db:
            with self.assertRaises(PermissionError):
                requeue_notification(db, notification_id, datetime.now() + timedelta(minutes=20), checker)
            self.assertEqual(get_notification(db, notification_id).status, NOTIFICATION_STATUS_FAILED)

    def test_requeue_preview_does_not_change_database(self):
        notification_id = self.create_notification(status=NOTIFICATION_STATUS_FAILED)
        new_time = datetime.now() + timedelta(minutes=20)
        with self.SessionLocal() as db:
            notif = get_notification(db, notification_id)
            preview = bot_runner.format_requeue_preview(notif, new_time, force=False)
            self.assertIn("Notification ID", preview)
            self.assertIn("повторно отправлена scheduler", preview)
            self.assertEqual(get_notification(db, notification_id).status, NOTIFICATION_STATUS_FAILED)
            self.assertEqual(db.query(AuditLog).filter(AuditLog.action == "notification_requeued").count(), 0)

    def test_requeue_handler_requires_admin_and_previews_without_confirm(self):
        class CommandMessage:
            def __init__(self, text):
                self.text = text
                self.answers = []

            async def answer(self, text, **kwargs):
                self.answers.append(text)

        notification_id = self.create_notification(status=NOTIFICATION_STATUS_FAILED)
        new_time = datetime.now() + timedelta(minutes=20)
        admin = SimpleNamespace(telegram_id="9001", username="admin", full_name="Admin", role=ROLE_ADMIN, is_active=True)
        original_session_local = bot_runner.SessionLocal
        original_get_admin = bot_runner.get_message_admin
        bot_runner.SessionLocal = self.SessionLocal
        try:
            checker_message = CommandMessage(f"/requeue {notification_id} {new_time:%Y-%m-%d} {new_time:%H:%M}")
            bot_runner.get_message_admin = lambda db, message: None
            asyncio.run(bot_runner.on_requeue(checker_message))
            self.assertEqual(checker_message.answers, ["Нет доступа"])

            admin_message = CommandMessage(f"/requeue {notification_id} {new_time:%Y-%m-%d} {new_time:%H:%M}")
            bot_runner.get_message_admin = lambda db, message: admin
            asyncio.run(bot_runner.on_requeue(admin_message))
            self.assertEqual(len(admin_message.answers), 1)
            self.assertIn("Preview requeue", admin_message.answers[0])
            with self.SessionLocal() as db:
                self.assertEqual(get_notification(db, notification_id).status, NOTIFICATION_STATUS_FAILED)
        finally:
            bot_runner.SessionLocal = original_session_local
            bot_runner.get_message_admin = original_get_admin

    def test_requeue_rejects_past_time(self):
        notification_id = self.create_notification(status=NOTIFICATION_STATUS_FAILED)
        admin = SimpleNamespace(telegram_id="9001", username="admin", full_name="Admin", role=ROLE_ADMIN, is_active=True)
        with self.SessionLocal() as db:
            with self.assertRaises(ValueError):
                requeue_notification(db, notification_id, datetime.now() - timedelta(minutes=1), admin)

    def test_archived_ppr_requires_force_for_requeue(self):
        notification_id = self.create_notification(status=NOTIFICATION_STATUS_FAILED, active=False, event_status=PPR_STATUS_ARCHIVED)
        admin = SimpleNamespace(telegram_id="9001", username="admin", full_name="Admin", role=ROLE_ADMIN, is_active=True)
        new_time = datetime.now() + timedelta(minutes=20)
        with self.SessionLocal() as db:
            with self.assertRaises(ValueError):
                requeue_notification(db, notification_id, new_time, admin)
            requeued = requeue_notification(db, notification_id, new_time, admin, force=True)
            self.assertTrue(requeued.event.is_active)
            self.assertEqual(requeued.event.ppr_status, PPR_STATUS_SCHEDULED)

    def test_admin_requeue_clears_delivery_metadata_and_writes_audit(self):
        notification_id = self.create_notification(status=NOTIFICATION_STATUS_FAILED)
        admin = SimpleNamespace(telegram_id="9001", username="admin", full_name="Admin", role=ROLE_ADMIN, is_active=True)
        new_time = datetime.now() + timedelta(minutes=20)
        with self.SessionLocal() as db:
            notif = get_notification(db, notification_id)
            notif.attempt_count = 2
            notif.last_error = "old error"
            notif.sent_at = datetime.now()
            notif.telegram_chat_id = "-1001"
            notif.telegram_message_id = "123"
            notif.processing_started_at = datetime.now()
            notif.processing_by = "old-worker"
            notif.processing_phase = "sending"
            notif.taken_by_id = "200"
            notif.taken_by_name = "Checker"
            notif.checked_by_id = "200"
            notif.checked_by_name = "Checker"
            notif.checked_at = datetime.now()
            notif.event.ppr_status = "in_progress"
            db.commit()

            requeued = requeue_notification(db, notification_id, new_time, admin)
            self.assertEqual(requeued.status, NOTIFICATION_STATUS_PLANNED)
            self.assertEqual(requeued.scheduled_at, new_time)
            self.assertTrue(requeued.auto_send_enabled)
            self.assertEqual(requeued.attempt_count, 0)
            for field in ("last_error", "sent_at", "telegram_chat_id", "telegram_message_id", "processing_started_at", "processing_by", "processing_phase", "taken_by_id", "checked_by_id"):
                self.assertIsNone(getattr(requeued, field))
            self.assertEqual(requeued.event.ppr_status, PPR_STATUS_SCHEDULED)
            audits = db.query(AuditLog).filter(AuditLog.notification_id == notification_id, AuditLog.action == "notification_requeued").all()
            self.assertEqual(len(audits), 1)
            self.assertIn("old_status=failed", audits[0].comment)
            self.assertIn("new_status=planned", audits[0].comment)

    def test_requeue_does_not_change_other_notifications_or_create_duplicate(self):
        first_id = self.create_notification(status=NOTIFICATION_STATUS_SENT)
        second_id = self.create_notification(status=NOTIFICATION_STATUS_FAILED)
        admin = SimpleNamespace(telegram_id="9001", username="admin", full_name="Admin", role=ROLE_ADMIN, is_active=True)
        new_time = datetime.now() + timedelta(minutes=20)
        with self.SessionLocal() as db:
            requeue_notification(db, first_id, new_time, admin)
            count = db.query(PprNotification).filter(PprNotification.ppr_event_id == get_notification(db, first_id).ppr_event_id).count()
            self.assertEqual(count, 1)
            self.assertEqual(get_notification(db, second_id).status, NOTIFICATION_STATUS_FAILED)
            requeue_notification(db, first_id, new_time + timedelta(minutes=1), admin)
            self.assertEqual(db.query(PprNotification).filter(PprNotification.ppr_event_id == get_notification(db, first_id).ppr_event_id).count(), 1)
            self.assertEqual(db.query(AuditLog).filter(AuditLog.notification_id == first_id, AuditLog.action == "notification_requeued").count(), 2)


if __name__ == "__main__":
    unittest.main()
