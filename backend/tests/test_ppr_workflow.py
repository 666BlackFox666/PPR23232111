import os
import tempfile
import threading
import unittest
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import AuditLog, PprEvent, PprNotification
from app.db.session import Base
from app.services.ppr_service import (
    WorkflowConflict,
    archive_ppr_event,
    check_notification,
    get_event_card,
    get_notification,
    take_notification,
    update_ppr_event,
)
from app.services.statuses import (
    NOTIFICATION_STATUS_CANCELLED,
    NOTIFICATION_STATUS_PLANNED,
    PPR_STATUS_ARCHIVED,
    PPR_STATUS_IN_PROGRESS,
    PPR_STATUS_SCHEDULED,
    PPR_STATUS_VERIFIED,
)


class PprWorkflowTestCase(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(prefix="ppr-workflow-", suffix=".db", delete=False)
        handle.close()
        self.db_path = handle.name
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False},
            future=True,
        )
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        Base.metadata.create_all(bind=self.engine)
        self.checker1 = SimpleNamespace(telegram_id="1001", username="checker1", full_name="Checker One", role="checker")
        self.checker2 = SimpleNamespace(telegram_id="1002", username="checker2", full_name="Checker Two", role="checker")
        self.admin = SimpleNamespace(telegram_id="9001", username="admin", full_name="Admin", role="admin")

    def tearDown(self):
        self.engine.dispose()
        try:
            os.remove(self.db_path)
        except FileNotFoundError:
            pass

    def create_notification(self):
        scheduled_at = datetime.combine(date.today() + timedelta(days=1), time(8, 0))
        with self.SessionLocal() as db:
            event = PprEvent(
                external_id=f"TEST-{uuid4()}",
                date=scheduled_at.date(),
                start_time=scheduled_at.time(),
                title="Test PPR",
                project="Tests",
                notify_start=True,
                is_active=True,
                ppr_status=PPR_STATUS_SCHEDULED,
            )
            db.add(event)
            db.flush()
            notif = PprNotification(
                ppr_event_id=event.id,
                type="start",
                scheduled_at=scheduled_at,
                status=NOTIFICATION_STATUS_PLANNED,
                auto_send_enabled=True,
            )
            db.add(notif)
            db.commit()
            return event.id, notif.id

    def audit_count(self, action):
        with self.SessionLocal() as db:
            return db.query(AuditLog).filter(AuditLog.action == action).count()

    def test_first_checker_takes_ppr(self):
        _, notification_id = self.create_notification()
        with self.SessionLocal() as db:
            ok, message, notif = take_notification(db, notification_id, self.checker1.telegram_id, "@checker1")
            self.assertTrue(ok)
            self.assertEqual(message, "Вы взяли ППР в работу")
            self.assertEqual(notif.event.ppr_status, PPR_STATUS_IN_PROGRESS)
            self.assertEqual(notif.taken_by_id, self.checker1.telegram_id)
        self.assertEqual(self.audit_count("take"), 1)

    def test_same_checker_take_is_idempotent(self):
        _, notification_id = self.create_notification()
        with self.SessionLocal() as db:
            self.assertTrue(take_notification(db, notification_id, self.checker1.telegram_id, "@checker1")[0])
            ok, message, notif = take_notification(db, notification_id, self.checker1.telegram_id, "@checker1")
            self.assertTrue(ok)
            self.assertEqual(message, "ППР уже находится у вас в работе")
            self.assertEqual(notif.taken_by_id, self.checker1.telegram_id)
        self.assertEqual(self.audit_count("take"), 1)

    def test_second_checker_gets_conflict(self):
        _, notification_id = self.create_notification()
        with self.SessionLocal() as db:
            self.assertTrue(take_notification(db, notification_id, self.checker1.telegram_id, "@checker1")[0])
            ok, message, _ = take_notification(db, notification_id, self.checker2.telegram_id, "@checker2")
            self.assertFalse(ok)
            self.assertIn("ППР уже взял", message)
        self.assertEqual(self.audit_count("take"), 1)

    def test_two_concurrent_take_requests_only_one_succeeds(self):
        _, notification_id = self.create_notification()
        barrier = threading.Barrier(2)
        results = []
        lock = threading.Lock()

        def run_take(user_id, user_name):
            barrier.wait()
            with self.SessionLocal() as db:
                result = take_notification(db, notification_id, user_id, user_name)
            with lock:
                results.append(result)

        t1 = threading.Thread(target=run_take, args=(self.checker1.telegram_id, "@checker1"))
        t2 = threading.Thread(target=run_take, args=(self.checker2.telegram_id, "@checker2"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(sum(1 for ok, _, _ in results if ok), 1)
        self.assertEqual(sum(1 for ok, _, _ in results if not ok), 1)
        self.assertEqual(self.audit_count("take"), 1)

    def test_verify_without_take_returns_conflict(self):
        _, notification_id = self.create_notification()
        with self.SessionLocal() as db:
            ok, message, _ = check_notification(db, notification_id, self.checker1.telegram_id, "@checker1")
            self.assertFalse(ok)
            self.assertEqual(message, "Сначала возьмите ППР в работу")

    def test_assigned_checker_verifies(self):
        _, notification_id = self.create_notification()
        with self.SessionLocal() as db:
            self.assertTrue(take_notification(db, notification_id, self.checker1.telegram_id, "@checker1")[0])
            ok, message, notif = check_notification(db, notification_id, self.checker1.telegram_id, "@checker1")
            self.assertTrue(ok)
            self.assertEqual(message, "ППР отмечена как проверенная")
            self.assertEqual(notif.event.ppr_status, PPR_STATUS_VERIFIED)
            self.assertEqual(notif.checked_by_id, self.checker1.telegram_id)
        self.assertEqual(self.audit_count("verify"), 1)

    def test_other_checker_cannot_verify(self):
        _, notification_id = self.create_notification()
        with self.SessionLocal() as db:
            self.assertTrue(take_notification(db, notification_id, self.checker1.telegram_id, "@checker1")[0])
            ok, message, _ = check_notification(db, notification_id, self.checker2.telegram_id, "@checker2")
            self.assertFalse(ok)
            self.assertIn("ППР находится в работе у", message)
        self.assertEqual(self.audit_count("verify"), 0)

    def test_admin_can_verify_assigned_ppr(self):
        _, notification_id = self.create_notification()
        with self.SessionLocal() as db:
            self.assertTrue(take_notification(db, notification_id, self.checker1.telegram_id, "@checker1")[0])
            ok, message, notif = check_notification(db, notification_id, self.admin.telegram_id, "@admin", is_admin=True)
            self.assertTrue(ok)
            self.assertEqual(message, "ППР отмечена как проверенная")
            self.assertEqual(notif.event.ppr_status, PPR_STATUS_VERIFIED)
            self.assertEqual(notif.checked_by_id, self.admin.telegram_id)

    def test_repeated_verify_does_not_duplicate_audit(self):
        _, notification_id = self.create_notification()
        with self.SessionLocal() as db:
            self.assertTrue(take_notification(db, notification_id, self.checker1.telegram_id, "@checker1")[0])
            self.assertTrue(check_notification(db, notification_id, self.checker1.telegram_id, "@checker1")[0])
            ok, message, _ = check_notification(db, notification_id, self.checker1.telegram_id, "@checker1")
            self.assertTrue(ok)
            self.assertEqual(message, "ППР уже проверена")
        self.assertEqual(self.audit_count("verify"), 1)

    def test_archived_ppr_cannot_be_taken_or_verified(self):
        event_id, notification_id = self.create_notification()
        with self.SessionLocal() as db:
            event = get_event_card(db, event_id)
            archive_ppr_event(db, event, self.admin)

        with self.SessionLocal() as db:
            ok, message, _ = take_notification(db, notification_id, self.checker1.telegram_id, "@checker1")
            self.assertFalse(ok)
            self.assertEqual(message, "Архивную ППР нельзя взять в работу")
            ok, message, _ = check_notification(db, notification_id, self.checker1.telegram_id, "@checker1")
            self.assertFalse(ok)
            self.assertEqual(message, "Архивную ППР нельзя проверить")

    def test_archive_in_progress_requires_force(self):
        event_id, notification_id = self.create_notification()
        with self.SessionLocal() as db:
            self.assertTrue(take_notification(db, notification_id, self.checker1.telegram_id, "@checker1")[0])
            event = get_event_card(db, event_id)
            with self.assertRaisesRegex(WorkflowConflict, "ППР находится в работе"):
                archive_ppr_event(db, event, self.admin)

    def test_archive_in_progress_with_force_works(self):
        event_id, notification_id = self.create_notification()
        with self.SessionLocal() as db:
            self.assertTrue(take_notification(db, notification_id, self.checker1.telegram_id, "@checker1")[0])
            event = get_event_card(db, event_id)
            archived = archive_ppr_event(db, event, self.admin, force=True)
            self.assertEqual(archived.ppr_status, PPR_STATUS_ARCHIVED)
            self.assertFalse(archived.is_active)
            notif = get_notification(db, notification_id)
            self.assertEqual(notif.status, NOTIFICATION_STATUS_CANCELLED)
        self.assertEqual(self.audit_count("forced_archive"), 1)

    def test_date_change_updates_existing_notification(self):
        event_id, _ = self.create_notification()
        with self.SessionLocal() as db:
            event = get_event_card(db, event_id)
            update_ppr_event(
                db,
                event,
                {"date": (date.today() + timedelta(days=3)).isoformat(), "start_time": "09:30"},
                self.admin,
            )
            count = db.query(PprNotification).filter(PprNotification.ppr_event_id == event_id, PprNotification.type == "start").count()
            self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
