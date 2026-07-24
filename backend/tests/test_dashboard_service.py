import os
import tempfile
import unittest
from datetime import date, datetime, time, timedelta

from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker

from app.db.models import PprEvent, PprNotification
from app.db.session import Base
from app.services.dashboard_service import (
    apply_event_filters,
    apply_search,
    apply_sort,
    base_event_query,
    dashboard_summary,
    list_ppr_events,
)
from app.services.statuses import (
    NOTIFICATION_STATUS_FAILED,
    NOTIFICATION_STATUS_PLANNED,
    PPR_STATUS_ARCHIVED,
    PPR_STATUS_IN_PROGRESS,
    PPR_STATUS_SCHEDULED,
    PPR_STATUS_VERIFIED,
)


class DashboardServiceTestCase(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(prefix="ppr-dashboard-", suffix=".db", delete=False)
        handle.close()
        self.db_path = handle.name
        self.engine = create_engine(f"sqlite:///{self.db_path}", connect_args={"check_same_thread": False}, future=True)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        Base.metadata.create_all(bind=self.engine)
        self.today = date.today()
        self.yesterday = self.today - timedelta(days=1)
        self.tomorrow = self.today + timedelta(days=1)

    def tearDown(self):
        self.engine.dispose()
        try:
            os.remove(self.db_path)
        except FileNotFoundError:
            pass

    def add_event(
        self,
        db,
        external_id,
        title,
        *,
        project="Project",
        event_date=None,
        start_time=time(8, 0),
        status=PPR_STATUS_SCHEDULED,
        active=True,
        notify=True,
        outlook_link=None,
        taken_by_id=None,
        taken_by_name=None,
        notification_status=NOTIFICATION_STATUS_PLANNED,
        activities="Work",
        responsible_report="Reporter",
        comment="Comment",
    ):
        event = PprEvent(
            external_id=external_id,
            source_key=f"id:{external_id}",
            title=title,
            project=project,
            date=event_date,
            start_time=start_time if event_date else None,
            ppr_status=status,
            is_active=active,
            notify_start=notify,
            outlook_link=outlook_link,
            activities=activities,
            responsible_report=responsible_report,
            comment=comment,
        )
        db.add(event)
        db.flush()
        if event_date:
            notif = PprNotification(
                ppr_event_id=event.id,
                type="start",
                scheduled_at=datetime.combine(event_date, start_time),
                status=notification_status,
                auto_send_enabled=True,
                taken_by_id=taken_by_id,
                taken_by_name=taken_by_name,
            )
            db.add(notif)
        return event

    def seed(self, db):
        self.add_event(db, "E1", "Alpha scheduled today", event_date=self.today, start_time=time(23, 59), project="A", status=PPR_STATUS_SCHEDULED)
        self.add_event(db, "E2", "Beta in work", event_date=self.yesterday, project="B", status=PPR_STATUS_IN_PROGRESS, taken_by_id="100", taken_by_name="@checker")
        self.add_event(db, "E3", "Gamma verified old", event_date=self.yesterday, project="B", status=PPR_STATUS_VERIFIED)
        self.add_event(db, "E4", "Delta missing date", event_date=None, project="C")
        self.add_event(db, "E5", "Epsilon archived", event_date=self.yesterday, active=False, status=PPR_STATUS_ARCHIVED)
        self.add_event(db, "E6", "Zeta failed", event_date=self.tomorrow, project="A", notification_status=NOTIFICATION_STATUS_FAILED)
        db.commit()

    def test_summary_counts_statuses_and_overdue_rules(self):
        with self.SessionLocal() as db:
            self.seed(db)
            summary = dashboard_summary(db, include_admin_counts=True)
            self.assertEqual(summary["today"], 1)
            self.assertEqual(summary["scheduled"], 3)
            self.assertEqual(summary["in_progress"], 1)
            self.assertEqual(summary["verified"], 1)
            self.assertEqual(summary["overdue"], 1)
            self.assertEqual(summary["missing_date"], 1)
            self.assertEqual(summary["notification_errors"], 1)
            self.assertEqual(summary["archived"], 1)

    def test_checker_summary_hides_archived_count(self):
        with self.SessionLocal() as db:
            self.seed(db)
            summary = dashboard_summary(db, include_admin_counts=False)
            self.assertEqual(summary["archived"], 0)

    def test_search_by_title_and_project(self):
        with self.SessionLocal() as db:
            self.seed(db)
            by_title = list_ppr_events(db, search="alpha", page_size=100)
            by_project = list_ppr_events(db, search="reporter", page_size=100)
            self.assertEqual(by_title["total"], 1)
            self.assertGreaterEqual(by_project["total"], 5)

    def test_combined_filters(self):
        with self.SessionLocal() as db:
            self.seed(db)
            result = list_ppr_events(db, status=PPR_STATUS_SCHEDULED, project="A", notify=True, outlook=False, page_size=100)
            self.assertEqual(result["total"], 2)

    def test_my_in_progress_filter(self):
        with self.SessionLocal() as db:
            self.seed(db)
            result = list_ppr_events(db, quick_filter="mine_in_progress", current_user_id="100", page_size=100)
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["items"][0]["event"]["title"], "Beta in work")

    def test_sorting(self):
        with self.SessionLocal() as db:
            self.seed(db)
            result = list_ppr_events(db, sort="title", page_size=100)
            titles = [item["event"]["title"] for item in result["items"]]
            self.assertEqual(titles, sorted(titles, key=str.lower))

    def test_pagination(self):
        with self.SessionLocal() as db:
            self.seed(db)
            first = list_ppr_events(db, page=1, page_size=2)
            second = list_ppr_events(db, page=2, page_size=2)
            self.assertEqual(first["total"], 5)
            self.assertEqual(len(first["items"]), 2)
            self.assertEqual(len(second["items"]), 2)
            self.assertNotEqual(first["items"][0]["event"]["id"], second["items"][0]["event"]["id"])

    def test_quick_filters_and_all_sorts_return_unique_events(self):
        with self.SessionLocal() as db:
            self.seed(db)
            failed_event = db.query(PprEvent).filter(PprEvent.external_id == "E6").one()
            # A second notification models the relation that used to multiply
            # PPR rows in the list query before DISTINCT was applied.
            db.add(
                PprNotification(
                    ppr_event_id=failed_event.id,
                    type="end",
                    scheduled_at=datetime.combine(self.tomorrow, time(12, 0)),
                    status=NOTIFICATION_STATUS_FAILED,
                    auto_send_enabled=True,
                )
            )
            db.commit()

            checks = {
                "today": {"quick_filter": "today", "sort": "date_asc"},
                "unverified": {"quick_filter": "unverified", "sort": "date_asc"},
                "overdue": {"quick_filter": "overdue", "sort": "overdue_first"},
                "notification_errors": {"quick_filter": "notification_errors", "sort": "date_asc"},
            }
            for name, params in checks.items():
                with self.subTest(quick_filter=name):
                    result = list_ppr_events(db, page_size=100, **params)
                    ids = [item["event"]["id"] for item in result["items"]]
                    self.assertEqual(len(ids), len(set(ids)))
                    self.assertEqual(result["total"], len(ids))

            overdue = list_ppr_events(db, quick_filter="overdue", sort="overdue_first", page_size=100)
            self.assertEqual([item["event"]["title"] for item in overdue["items"]], ["Beta in work"])
            errors = list_ppr_events(db, quick_filter="notification_errors", page_size=100)
            self.assertEqual(errors["total"], 1)
            self.assertEqual(errors["items"][0]["event"]["title"], "Zeta failed")

            for sort in ("date_asc", "date_desc", "overdue_first", "updated_desc", "title", "project"):
                with self.subTest(sort=sort):
                    result = list_ppr_events(db, sort=sort, page_size=100)
                    ids = [item["event"]["id"] for item in result["items"]]
                    self.assertEqual(len(ids), len(set(ids)))
                    self.assertEqual(result["total"], len(ids))

    def test_postgresql_list_query_uses_exists_without_distinct(self):
        with self.SessionLocal() as db:
            self.seed(db)
            query = base_event_query(db, load_related=False)
            query = apply_search(query, "checker")
            query = apply_event_filters(query, quick_filter="notification_errors")
            statement = apply_sort(query.with_entities(PprEvent.id), "date_asc").statement
            sql = str(statement.compile(dialect=postgresql.dialect()))
            self.assertNotIn("DISTINCT", sql.upper())
            self.assertIn("EXISTS", sql.upper())

    def test_old_endpoint_queries_can_still_be_represented(self):
        with self.SessionLocal() as db:
            self.seed(db)
            today = list_ppr_events(db, quick_filter="today", page_size=100)
            missing = list_ppr_events(db, quick_filter="missing_date", page_size=100)
            unverified = list_ppr_events(db, quick_filter="unverified", page_size=100)
            self.assertEqual(today["total"], 1)
            self.assertEqual(missing["total"], 1)
            self.assertGreaterEqual(unverified["total"], 3)


if __name__ == "__main__":
    unittest.main()
