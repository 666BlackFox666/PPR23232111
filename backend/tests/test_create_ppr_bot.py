import asyncio
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.bot import runner
from app.db.models import AppUser, AuditLog, PprEvent, PprNotification
from app.db.session import Base
from app.services.create_ppr_service import (
    CREATE_PPR_USAGE,
    CreatePprPreviewAccessDenied,
    CreatePprPreviewCapacityExceeded,
    CreatePprPreviewExpired,
    CreatePprPreviewNotFound,
    CreatePprPreviewStore,
    CreatePprValidationError,
    parse_create_ppr_command,
)
from app.services.ppr_service import (
    PprDuplicateError,
    _acquire_create_duplicate_lock,
    _create_duplicate_lock_key,
    create_ppr_event,
    find_active_ppr_duplicate,
)
from app.services.user_service import ROLE_ADMIN, ROLE_CHECKER


VALID_COMMAND = (
    "/createppr 2099-08-03 09:00 | ППР инфраструктуры | "
    "Проверка резервного копирования | Проверить backup, журнал и восстановление"
)


class FakeUser:
    def __init__(self, user_id: int, username: str = "user"):
        self.id = user_id
        self.username = username
        self.first_name = "Test"
        self.last_name = "User"


class FakeChat:
    def __init__(self, chat_id: int):
        self.id = chat_id


class FakeMessage:
    def __init__(self, text: str, user_id: int, chat_id: int = -1001):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.chat = FakeChat(chat_id)
        self.answers = []
        self.edits = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return self

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))
        return self


class FakeCallback:
    def __init__(self, data: str, user_id: int, chat_id: int = -1001):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage("", user_id, chat_id)
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


class CreatePprParserTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 30, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))

    def parse(self, text: str):
        return parse_create_ppr_command(
            text,
            timezone_name="Europe/Moscow",
            now=self.now,
        )

    def test_valid_command_and_empty_activities(self):
        draft = self.parse(
            "/createppr 2026-08-03 09:00 | Project | Title |"
        )
        self.assertEqual(draft.project, "Project")
        self.assertEqual(draft.title, "Title")
        self.assertIsNone(draft.activities)
        self.assertEqual(draft.scheduled_at.isoformat(), "2026-08-03T09:00:00")
        self.assertTrue(draft.auto_send_enabled)

    def test_invalid_format_and_extra_separator(self):
        for command in (
            "/createppr",
            "/createppr 2026-08-03 09:00 | Project | Title",
            "/createppr 2026-08-03 09:00 | Project | Title | Work | Extra",
        ):
            with self.subTest(command=command), self.assertRaises(
                CreatePprValidationError
            ) as ctx:
                self.parse(command)
            self.assertIn("Использование", str(ctx.exception))

    def test_invalid_date(self):
        with self.assertRaisesRegex(
            CreatePprValidationError, "несуществующая календарная дата"
        ):
            self.parse("/createppr 2026-02-30 09:00 | Project | Title | Work")

    def test_invalid_time(self):
        with self.assertRaisesRegex(CreatePprValidationError, "00:00–23:59"):
            self.parse("/createppr 2026-08-03 24:00 | Project | Title | Work")

    def test_strict_date_and_time_format(self):
        for command in (
            "/createppr 03-08-2026 09:00 | Project | Title | Work",
            "/createppr 2026-08-03 9:00 | Project | Title | Work",
        ):
            with self.subTest(command=command), self.assertRaises(
                CreatePprValidationError
            ):
                self.parse(command)

    def test_past_time_is_rejected_in_configured_timezone(self):
        with self.assertRaisesRegex(CreatePprValidationError, "в будущем"):
            self.parse("/createppr 2026-07-30 11:59 | Project | Title | Work")

    def test_time_exactly_now_is_rejected(self):
        with self.assertRaisesRegex(CreatePprValidationError, "в будущем"):
            self.parse("/createppr 2026-07-30 12:00 | Project | Title | Work")

    def test_naive_now_is_interpreted_in_configured_timezone(self):
        draft = parse_create_ppr_command(
            "/createppr 2026-07-30 12:01 | Project | Title | Work",
            timezone_name="Europe/Moscow",
            now=datetime(2026, 7, 30, 12, 0),
        )
        self.assertEqual(draft.scheduled_at, datetime(2026, 7, 30, 12, 1))

    def test_valid_leap_day_is_accepted(self):
        draft = parse_create_ppr_command(
            "/createppr 2028-02-29 09:00 | Project | Leap day | Work",
            timezone_name="Europe/Moscow",
            now=self.now,
        )
        self.assertEqual(draft.event_date, date(2028, 2, 29))

    def test_nonexistent_dst_time_is_rejected(self):
        with self.assertRaisesRegex(CreatePprValidationError, "не существует"):
            parse_create_ppr_command(
                "/createppr 2026-03-29 02:30 | Project | DST gap | Work",
                timezone_name="Europe/Berlin",
                now=datetime(2026, 1, 1, tzinfo=ZoneInfo("Europe/Berlin")),
            )

    def test_ambiguous_dst_time_is_rejected(self):
        with self.assertRaisesRegex(CreatePprValidationError, "неоднозначно"):
            parse_create_ppr_command(
                "/createppr 2026-10-25 02:30 | Project | DST fold | Work",
                timezone_name="Europe/Berlin",
                now=datetime(2026, 1, 1, tzinfo=ZoneInfo("Europe/Berlin")),
            )

    def test_empty_project_and_title(self):
        with self.assertRaisesRegex(CreatePprValidationError, "Проект"):
            self.parse("/createppr 2026-08-03 09:00 | | Title | Work")
        with self.assertRaisesRegex(CreatePprValidationError, "Название"):
            self.parse("/createppr 2026-08-03 09:00 | Project | | Work")

    def test_fields_are_normalized(self):
        draft = self.parse(
            "/createppr 2026-08-03 09:00 |  My   Project  |  My   Title  |  One   two "
        )
        self.assertEqual(draft.project, "My Project")
        self.assertEqual(draft.title, "My Title")
        self.assertEqual(draft.activities, "One two")

    def test_overlong_project_is_rejected(self):
        with self.assertRaisesRegex(CreatePprValidationError, "255"):
            self.parse(
                f"/createppr 2026-08-03 09:00 | {'P' * 256} | Title | Work"
            )

    def test_usage_constant_contains_exact_syntax(self):
        self.assertIn(
            "/createppr YYYY-MM-DD HH:MM | Проект | Название ППР | Активности",
            CREATE_PPR_USAGE,
        )


class CreatePprPreviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.draft = parse_create_ppr_command(
            VALID_COMMAND,
            timezone_name="Europe/Moscow",
            now=datetime(2026, 7, 30, tzinfo=ZoneInfo("Europe/Moscow")),
        )
        self.now = datetime.now(timezone.utc)

    def test_preview_is_bound_to_user_and_chat_and_consumed_once(self):
        store = CreatePprPreviewStore()
        preview = store.create(
            self.draft,
            telegram_user_id="1",
            chat_id="-100",
            now=self.now,
        )
        with self.assertRaises(CreatePprPreviewAccessDenied):
            store.consume(
                preview.token,
                telegram_user_id="2",
                chat_id="-100",
                now=self.now,
            )
        with self.assertRaises(CreatePprPreviewAccessDenied):
            store.consume(
                preview.token,
                telegram_user_id="1",
                chat_id="-200",
                now=self.now,
            )
        consumed = store.consume(
            preview.token,
            telegram_user_id="1",
            chat_id="-100",
            now=self.now,
        )
        self.assertEqual(consumed.token, preview.token)
        with self.assertRaises(CreatePprPreviewNotFound):
            store.consume(
                preview.token,
                telegram_user_id="1",
                chat_id="-100",
                now=self.now,
            )

    def test_expired_preview_is_removed(self):
        store = CreatePprPreviewStore(ttl=timedelta(minutes=10))
        preview = store.create(
            self.draft,
            telegram_user_id="1",
            chat_id="-100",
            now=self.now,
        )
        with self.assertRaises(CreatePprPreviewExpired):
            store.consume(
                preview.token,
                telegram_user_id="1",
                chat_id="-100",
                now=self.now + timedelta(minutes=10),
            )
        self.assertEqual(len(store), 0)

    def test_cancel_invalidates_preview(self):
        store = CreatePprPreviewStore()
        preview = store.create(
            self.draft,
            telegram_user_id="1",
            chat_id="-100",
            now=self.now,
        )
        store.cancel(
            preview.token,
            telegram_user_id="1",
            chat_id="-100",
            now=self.now,
        )
        with self.assertRaises(CreatePprPreviewNotFound):
            store.consume(
                preview.token,
                telegram_user_id="1",
                chat_id="-100",
                now=self.now,
            )

    def test_two_admins_have_independent_previews(self):
        store = CreatePprPreviewStore()
        first = store.create(
            self.draft, telegram_user_id="1", chat_id="-100", now=self.now
        )
        second = store.create(
            self.draft, telegram_user_id="2", chat_id="-200", now=self.now
        )
        self.assertNotEqual(first.token, second.token)
        self.assertEqual(len(store), 2)

    def test_store_capacity_rejects_new_preview_without_evicting_active_ones(self):
        store = CreatePprPreviewStore(max_entries=2)
        first = store.create(
            self.draft,
            telegram_user_id="1",
            chat_id="-100",
            now=self.now,
        )
        second = store.create(
            self.draft,
            telegram_user_id="2",
            chat_id="-100",
            now=self.now + timedelta(seconds=1),
        )
        with self.assertRaises(CreatePprPreviewCapacityExceeded):
            store.create(
                self.draft,
                telegram_user_id="3",
                chat_id="-100",
                now=self.now + timedelta(seconds=2),
            )
        self.assertEqual(len(store), 2)
        self.assertEqual(
            store.consume(
                first.token,
                telegram_user_id="1",
                chat_id="-100",
                now=self.now + timedelta(seconds=2),
            ).token,
            first.token,
        )
        self.assertEqual(
            store.consume(
                second.token,
                telegram_user_id="2",
                chat_id="-100",
                now=self.now + timedelta(seconds=2),
            ).token,
            second.token,
        )

    def test_two_threads_can_consume_preview_only_once(self):
        store = CreatePprPreviewStore()
        preview = store.create(
            self.draft,
            telegram_user_id="1",
            chat_id="-100",
            now=self.now,
        )

        def consume():
            try:
                store.consume(
                    preview.token,
                    telegram_user_id="1",
                    chat_id="-100",
                    now=self.now,
                )
                return "consumed"
            except CreatePprPreviewNotFound:
                return "missing"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: consume(), range(2)))

        self.assertEqual(results.count("consumed"), 1)
        self.assertEqual(results.count("missing"), 1)


class CreatePprServiceTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(
            prefix="ppr-create-service-", suffix=".db", delete=False
        )
        handle.close()
        self.db_path = handle.name
        self.engine = create_engine(f"sqlite:///{self.db_path}", future=True)
        self.SessionLocal = sessionmaker(
            bind=self.engine, expire_on_commit=False, future=True
        )
        Base.metadata.create_all(self.engine)
        self.admin = SimpleNamespace(
            telegram_id="9001",
            username="admin",
            full_name="Admin",
            role=ROLE_ADMIN,
        )
        self.draft = parse_create_ppr_command(
            VALID_COMMAND,
            timezone_name="Europe/Moscow",
            now=datetime(2026, 7, 30, tzinfo=ZoneInfo("Europe/Moscow")),
        )

    def tearDown(self):
        self.engine.dispose()
        os.remove(self.db_path)

    def test_create_uses_shared_service_and_writes_one_event_notification_and_audit(self):
        with self.SessionLocal() as db:
            event = create_ppr_event(db, self.draft.payload(), self.admin)
            self.assertEqual(db.query(PprEvent).count(), 1)
            self.assertEqual(db.query(PprNotification).count(), 1)
            notification = db.query(PprNotification).one()
            self.assertEqual(notification.type, "start")
            self.assertEqual(notification.status, "planned")
            self.assertTrue(notification.auto_send_enabled)
            self.assertEqual(event.ppr_status, "scheduled")
            self.assertTrue(event.notify_start)
            self.assertFalse(event.notify_end)
            audit = (
                db.query(AuditLog)
                .filter(AuditLog.action == "created")
                .one()
            )
            self.assertEqual(audit.user_id, "9001")
            self.assertEqual(audit.user_name, "@admin")

    def test_normalized_duplicate_is_rejected_without_second_notification(self):
        with self.SessionLocal() as db:
            create_ppr_event(db, self.draft.payload(), self.admin)
            duplicate_payload = {
                **self.draft.payload(),
                "title": "  ПРОВЕРКА   РЕЗЕРВНОГО КОПИРОВАНИЯ ",
                "project": " ппр   ИНФРАСТРУКТУРЫ ",
            }
            with self.assertRaises(PprDuplicateError) as ctx:
                create_ppr_event(db, duplicate_payload, self.admin)
            self.assertEqual(ctx.exception.event_id, 1)
            self.assertEqual(db.query(PprEvent).count(), 1)
            self.assertEqual(db.query(PprNotification).count(), 1)

    def test_find_duplicate_ignores_archived_event(self):
        with self.SessionLocal() as db:
            event = create_ppr_event(db, self.draft.payload(), self.admin)
            event.is_active = False
            db.commit()
            duplicate = find_active_ppr_duplicate(
                db,
                title=self.draft.title,
                project=self.draft.project,
                event_date=self.draft.event_date,
                start_time=self.draft.start_time,
            )
            self.assertIsNone(duplicate)

    def test_create_rolls_back_event_audit_and_notification_on_failure(self):
        with self.SessionLocal() as db, patch(
            "app.services.ppr_service.sync_start_notification",
            side_effect=RuntimeError("notification failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "notification failed"):
                create_ppr_event(db, self.draft.payload(), self.admin)

            self.assertEqual(db.query(PprEvent).count(), 0)
            self.assertEqual(db.query(PprNotification).count(), 0)
            self.assertEqual(db.query(AuditLog).count(), 0)

    def test_duplicate_check_runs_after_lock(self):
        calls = []

        def lock(*args, **kwargs):
            calls.append("lock")

        def duplicate(*args, **kwargs):
            calls.append("duplicate")
            return None

        with self.SessionLocal() as db, patch(
            "app.services.ppr_service._acquire_create_duplicate_lock",
            side_effect=lock,
        ), patch(
            "app.services.ppr_service.find_active_ppr_duplicate",
            side_effect=duplicate,
        ):
            create_ppr_event(db, self.draft.payload(), self.admin)

        self.assertEqual(calls[:2], ["lock", "duplicate"])


class CreatePprAdvisoryLockTests(unittest.TestCase):
    @staticmethod
    def lock_key(**overrides) -> int:
        values = {
            "title": "Maintenance",
            "project": "Core",
            "event_date": date(2026, 8, 3),
            "start_time": time(9, 0),
        }
        values.update(overrides)
        return _create_duplicate_lock_key(**values)

    @staticmethod
    def fake_db(dialect_name: str) -> MagicMock:
        db = MagicMock()
        db.get_bind.return_value = SimpleNamespace(
            dialect=SimpleNamespace(name=dialect_name)
        )
        return db

    def test_lock_key_is_stable_normalized_and_signed_bigint(self):
        first = self.lock_key()
        same = self.lock_key(title="  MAINTENANCE ", project=" core ")
        different = self.lock_key(start_time=time(9, 1))

        self.assertEqual(first, same)
        self.assertNotEqual(first, different)
        self.assertGreaterEqual(first, -(2**63))
        self.assertLessEqual(first, 2**63 - 1)

    def test_postgresql_uses_transaction_level_advisory_lock(self):
        db = self.fake_db("postgresql")

        _acquire_create_duplicate_lock(
            db,
            title="Maintenance",
            project="Core",
            event_date=date(2026, 8, 3),
            start_time=time(9, 0),
        )

        db.execute.assert_called_once()
        statement, params = db.execute.call_args.args
        self.assertIn("pg_advisory_xact_lock", str(statement))
        self.assertEqual(params["lock_key"], self.lock_key())

    def test_sqlite_skips_advisory_lock(self):
        db = self.fake_db("sqlite")

        _acquire_create_duplicate_lock(
            db,
            title="Maintenance",
            project="Core",
            event_date=date(2026, 8, 3),
            start_time=time(9, 0),
        )

        db.execute.assert_not_called()

    def test_postgresql_lock_error_is_not_masked(self):
        db = self.fake_db("postgresql")
        db.execute.side_effect = RuntimeError("lock failed")

        with self.assertRaisesRegex(RuntimeError, "lock failed"):
            _acquire_create_duplicate_lock(
                db,
                title="Maintenance",
                project="Core",
                event_date=date(2026, 8, 3),
                start_time=time(9, 0),
            )


class CreatePprBotHandlerTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(
            prefix="ppr-create-bot-", suffix=".db", delete=False
        )
        handle.close()
        self.db_path = handle.name
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False},
            future=True,
        )
        self.SessionLocal = sessionmaker(
            bind=self.engine, expire_on_commit=False, future=True
        )
        Base.metadata.create_all(self.engine)
        with self.SessionLocal() as db:
            db.add_all(
                [
                    AppUser(
                        telegram_id="9001",
                        username="admin",
                        role=ROLE_ADMIN,
                        is_active=True,
                    ),
                    AppUser(
                        telegram_id="9002",
                        username="checker",
                        role=ROLE_CHECKER,
                        is_active=True,
                    ),
                    AppUser(
                        telegram_id="9003",
                        username="admin2",
                        role=ROLE_ADMIN,
                        is_active=True,
                    ),
                    AppUser(
                        telegram_id="9004",
                        username="inactive-admin",
                        role=ROLE_ADMIN,
                        is_active=False,
                    ),
                ]
            )
            db.commit()
        self.store = CreatePprPreviewStore()
        self.session_patch = patch.object(
            runner, "SessionLocal", self.SessionLocal
        )
        self.store_patch = patch.object(
            runner, "create_ppr_previews", self.store
        )
        self.session_patch.start()
        self.store_patch.start()

    def tearDown(self):
        self.store_patch.stop()
        self.session_patch.stop()
        self.engine.dispose()
        os.remove(self.db_path)

    def create_preview(self, user_id: int = 9001, chat_id: int = -1001):
        message = FakeMessage(VALID_COMMAND, user_id, chat_id)
        asyncio.run(runner.on_createppr(message))
        return message

    @staticmethod
    def callback_data(message: FakeMessage, action: str) -> str:
        keyboard = message.answers[-1][1]["reply_markup"]
        button_index = 0 if action == "confirm" else 1
        return keyboard.inline_keyboard[0][button_index].callback_data

    def test_admin_gets_preview_without_database_writes_and_html_is_escaped(self):
        message = FakeMessage(
            "/createppr 2099-08-03 09:00 | <Project> | <Title> | <Work>",
            9001,
        )
        asyncio.run(runner.on_createppr(message))
        text, kwargs = message.answers[-1]
        self.assertIn("&lt;Project&gt;", text)
        self.assertIn("&lt;Title&gt;", text)
        self.assertNotIn("<Project>", text)
        self.assertIn("✅ Создать", kwargs["reply_markup"].inline_keyboard[0][0].text)
        for button in kwargs["reply_markup"].inline_keyboard[0]:
            self.assertLessEqual(len(button.callback_data.encode("utf-8")), 64)
        with self.SessionLocal() as db:
            self.assertEqual(db.query(PprEvent).count(), 0)
            self.assertEqual(db.query(PprNotification).count(), 0)

    def test_preview_too_long_for_telegram_is_rejected_before_store(self):
        title = "😀" * 80
        activities = "😀" * 1900
        command = f"/createppr 2099-08-03 09:00 | P | {title} | {activities}"
        self.assertLessEqual(len(command.encode("utf-16-le")) // 2, 4096)
        message = FakeMessage(command, 9001)

        asyncio.run(runner.on_createppr(message))

        self.assertIn("слишком длинный", message.answers[-1][0])
        self.assertEqual(len(self.store), 0)

    def test_full_preview_store_returns_clear_error_without_evicting_first(self):
        self.store.max_entries = 1
        first = self.create_preview()
        first_data = self.callback_data(first, "confirm")
        second = FakeMessage(
            "/createppr 2099-08-04 10:00 | Other | Other title | Work",
            9003,
            -2002,
        )

        asyncio.run(runner.on_createppr(second))

        self.assertIn("Слишком много активных Preview", second.answers[-1][0])
        self.assertEqual(len(self.store), 1)
        callback = FakeCallback(first_data, 9001)
        asyncio.run(runner.on_createppr_confirm(callback))
        self.assertIn("ППР создана", callback.message.edits[-1][0])

    def test_checker_inactive_admin_and_unknown_user_get_no_access(self):
        for user_id in (9002, 9004, 9999):
            with self.subTest(user_id=user_id):
                message = FakeMessage(VALID_COMMAND, user_id)
                asyncio.run(runner.on_createppr(message))
                self.assertEqual(message.answers[-1][0], "Нет доступа")
        self.assertEqual(len(self.store), 0)

    def test_invalid_command_returns_usage(self):
        message = FakeMessage("/createppr broken", 9001)
        asyncio.run(runner.on_createppr(message))
        self.assertIn("Использование", message.answers[-1][0])
        self.assertEqual(len(self.store), 0)

    def test_confirm_creates_exactly_once_and_second_confirm_is_idempotent(self):
        message = self.create_preview()
        data = self.callback_data(message, "confirm")
        callback = FakeCallback(data, 9001)
        asyncio.run(runner.on_createppr_confirm(callback))
        self.assertIn("ППР создана", callback.message.edits[-1][0])
        self.assertIn("Notification ID", callback.message.edits[-1][0])

        second = FakeCallback(data, 9001)
        asyncio.run(runner.on_createppr_confirm(second))
        self.assertIn("уже был использован", second.answers[-1][0])
        with self.SessionLocal() as db:
            self.assertEqual(db.query(PprEvent).count(), 1)
            self.assertEqual(db.query(PprNotification).count(), 1)
            self.assertEqual(
                db.query(AuditLog)
                .filter(AuditLog.action == "created", AuditLog.user_id == "9001")
                .count(),
                1,
            )

    def test_two_simultaneous_confirm_callbacks_create_once(self):
        message = self.create_preview()
        data = self.callback_data(message, "confirm")
        first = FakeCallback(data, 9001)
        second = FakeCallback(data, 9001)

        async def confirm_both():
            await asyncio.gather(
                runner.on_createppr_confirm(first),
                runner.on_createppr_confirm(second),
            )

        asyncio.run(confirm_both())

        self.assertEqual(
            sum(bool(callback.message.edits) for callback in (first, second)),
            1,
        )
        self.assertEqual(
            sum(
                any("уже был использован" in (text or "") for text, _ in callback.answers)
                for callback in (first, second)
            ),
            1,
        )
        with self.SessionLocal() as db:
            self.assertEqual(db.query(PprEvent).count(), 1)
            self.assertEqual(db.query(PprNotification).count(), 1)

    def test_inactive_admin_cannot_confirm_existing_preview(self):
        message = self.create_preview()
        data = self.callback_data(message, "confirm")
        with self.SessionLocal() as db:
            admin = db.query(AppUser).filter(AppUser.telegram_id == "9001").one()
            admin.is_active = False
            db.commit()

        callback = FakeCallback(data, 9001)
        asyncio.run(runner.on_createppr_confirm(callback))

        self.assertEqual(callback.answers[-1][0], "Нет доступа")
        with self.SessionLocal() as db:
            self.assertEqual(db.query(PprEvent).count(), 0)
            self.assertEqual(db.query(PprNotification).count(), 0)

    def test_confirm_by_other_admin_or_other_chat_is_forbidden(self):
        message = self.create_preview()
        data = self.callback_data(message, "confirm")
        other_admin = FakeCallback(data, 9003)
        asyncio.run(runner.on_createppr_confirm(other_admin))
        self.assertIn("только создавший", other_admin.answers[-1][0])

        other_chat = FakeCallback(data, 9001, chat_id=-2002)
        asyncio.run(runner.on_createppr_confirm(other_chat))
        self.assertIn("исходном чате", other_chat.answers[-1][0])
        with self.SessionLocal() as db:
            self.assertEqual(db.query(PprEvent).count(), 0)

    def test_cancel_prevents_confirmation(self):
        message = self.create_preview()
        confirm_data = self.callback_data(message, "confirm")
        cancel_data = self.callback_data(message, "cancel")
        cancel = FakeCallback(cancel_data, 9001)
        asyncio.run(runner.on_createppr_cancel(cancel))
        self.assertIn("отменено", cancel.message.edits[-1][0].lower())

        confirm = FakeCallback(confirm_data, 9001)
        asyncio.run(runner.on_createppr_confirm(confirm))
        self.assertIn("уже был использован", confirm.answers[-1][0])
        with self.SessionLocal() as db:
            self.assertEqual(db.query(PprEvent).count(), 0)

    def test_expired_preview_is_rejected(self):
        draft = parse_create_ppr_command(
            VALID_COMMAND,
            timezone_name="Europe/Moscow",
            now=datetime(2026, 7, 30, tzinfo=ZoneInfo("Europe/Moscow")),
        )
        preview = self.store.create(
            draft,
            telegram_user_id="9001",
            chat_id="-1001",
            now=datetime.now(timezone.utc) - timedelta(minutes=11),
        )
        callback = FakeCallback(f"createppr:confirm:{preview.token}", 9001)
        asyncio.run(runner.on_createppr_confirm(callback))
        self.assertIn("истёк", callback.answers[-1][0])
        with self.SessionLocal() as db:
            self.assertEqual(db.query(PprEvent).count(), 0)

    def test_duplicate_is_blocked_before_preview(self):
        draft = parse_create_ppr_command(
            VALID_COMMAND,
            timezone_name="Europe/Moscow",
            now=datetime(2026, 7, 30, tzinfo=ZoneInfo("Europe/Moscow")),
        )
        with self.SessionLocal() as db:
            admin = db.query(AppUser).filter(AppUser.telegram_id == "9001").one()
            create_ppr_event(db, draft.payload(), admin)
        message = self.create_preview()
        self.assertIn("дублирующая запись ID", message.answers[-1][0])
        self.assertEqual(len(self.store), 0)

    def test_duplicate_appearing_between_preview_and_confirm_is_blocked(self):
        message = self.create_preview()
        data = self.callback_data(message, "confirm")
        draft = parse_create_ppr_command(
            VALID_COMMAND,
            timezone_name="Europe/Moscow",
            now=datetime(2026, 7, 30, tzinfo=ZoneInfo("Europe/Moscow")),
        )
        with self.SessionLocal() as db:
            admin = db.query(AppUser).filter(AppUser.telegram_id == "9001").one()
            create_ppr_event(db, draft.payload(), admin)

        callback = FakeCallback(data, 9001)
        asyncio.run(runner.on_createppr_confirm(callback))
        self.assertIn("дублирующая запись ID", callback.answers[-1][0])
        with self.SessionLocal() as db:
            self.assertEqual(db.query(PprEvent).count(), 1)
            self.assertEqual(db.query(PprNotification).count(), 1)

    def test_two_admins_can_hold_concurrent_previews(self):
        first = self.create_preview(9001, -1001)
        second = FakeMessage(
            "/createppr 2099-08-04 10:00 | Other | Other title | Work",
            9003,
            -2002,
        )
        asyncio.run(runner.on_createppr(second))
        self.assertEqual(len(self.store), 2)
        self.assertNotEqual(
            self.callback_data(first, "confirm"),
            self.callback_data(second, "confirm"),
        )

    def test_existing_ping_and_help_handlers_still_respond(self):
        ping = FakeMessage("/ping", 9001)
        asyncio.run(runner.on_ping(ping))
        self.assertEqual(ping.answers[-1][0], "pong")

        help_message = FakeMessage("/help", 9001)
        with patch.object(
            runner, "outlook_integration_configured", return_value=False
        ):
            asyncio.run(runner.on_help(help_message))
        self.assertIn("/createppr", help_message.answers[-1][0])


if __name__ == "__main__":
    unittest.main()
