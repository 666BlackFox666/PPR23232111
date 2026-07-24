from datetime import date, datetime, time
from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, JSON, String, Text, Time, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.session import Base


class AppUser(Base):
    __tablename__ = "app_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(32), default="checker", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PprEvent(Base):
    __tablename__ = "ppr_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    source_key: Mapped[str | None] = mapped_column(String(255), unique=True, index=True, nullable=True)
    source_row: Mapped[int | None] = mapped_column(Integer, nullable=True)

    date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    end_time: Mapped[time | None] = mapped_column(Time, nullable=True)

    notification_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    project: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    title: Mapped[str] = mapped_column(Text)
    activities: Mapped[str | None] = mapped_column(Text, nullable=True)
    responsible_setup: Mapped[str | None] = mapped_column(String(255), nullable=True)
    responsible_report: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    outlook_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    notify_start: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_end: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    ppr_status: Mapped[str] = mapped_column(String(32), default="scheduled", index=True)
    is_manually_edited: Mapped[bool] = mapped_column(Boolean, default=False)
    manual_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    notifications: Mapped[list["PprNotification"]] = relationship(back_populates="event", cascade="all, delete-orphan")
    audit_logs: Mapped[list["AuditLog"]] = relationship(back_populates="event", cascade="all, delete-orphan")

    @property
    def outlook_url(self) -> str | None:
        return self.outlook_link

    @outlook_url.setter
    def outlook_url(self, value: str | None) -> None:
        self.outlook_link = value


class PprNotification(Base):
    __tablename__ = "ppr_notifications"
    __table_args__ = (UniqueConstraint("ppr_event_id", "type", name="uq_event_notification_type"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ppr_event_id: Mapped[int] = mapped_column(ForeignKey("ppr_events.id"), index=True)
    type: Mapped[str] = mapped_column(String(16))  # start/end
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(32), default="planned", index=True)  # planned/sent/failed/skipped/cancelled
    auto_send_enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    telegram_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    processing_by: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    processing_phase: Mapped[str | None] = mapped_column(String(32), nullable=True)
    telegram_edit_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    reminder_count: Mapped[int] = mapped_column(Integer, default=0)
    last_reminder_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    taken_by_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    taken_by_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    taken_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    checked_by_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    checked_by_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    event: Mapped[PprEvent] = relationship(back_populates="notifications")
    audit_logs: Mapped[list["AuditLog"]] = relationship(back_populates="notification", cascade="all, delete-orphan")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    notification_id: Mapped[int | None] = mapped_column(ForeignKey("ppr_notifications.id"), index=True, nullable=True)
    ppr_event_id: Mapped[int | None] = mapped_column(ForeignKey("ppr_events.id"), index=True, nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    notification: Mapped[PprNotification] = relationship(back_populates="audit_logs")
    event: Mapped[PprEvent] = relationship(back_populates="audit_logs")


class ImportRun(Base):
    __tablename__ = "import_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    preview_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True, nullable=True)
    filename: Mapped[str] = mapped_column(String(255))
    mode: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_by_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_by_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_hash: Mapped[str] = mapped_column(String(64), index=True)


class SchedulerHeartbeat(Base):
    __tablename__ = "scheduler_heartbeats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    worker_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_successful_poll_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_poll_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_running: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
