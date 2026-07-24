"""create ppr tables

Revision ID: 20260708_0001
Revises:
Create Date: 2026-07-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260708_0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ppr_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("source_row", sa.Integer(), nullable=True),
        sa.Column("date", sa.Date(), nullable=True),
        sa.Column("start_time", sa.Time(), nullable=True),
        sa.Column("end_time", sa.Time(), nullable=True),
        sa.Column("notification_type", sa.String(length=64), nullable=True),
        sa.Column("project", sa.String(length=255), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("activities", sa.Text(), nullable=True),
        sa.Column("responsible_setup", sa.String(length=255), nullable=True),
        sa.Column("responsible_report", sa.String(length=255), nullable=True),
        sa.Column("source_link", sa.Text(), nullable=True),
        sa.Column("outlook_link", sa.Text(), nullable=True),
        sa.Column("notify_start", sa.Boolean(), nullable=False),
        sa.Column("notify_end", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ppr_events_external_id", "ppr_events", ["external_id"], unique=True)

    op.create_table(
        "ppr_notifications",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ppr_event_id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("telegram_chat_id", sa.String(length=64), nullable=True),
        sa.Column("telegram_message_id", sa.String(length=64), nullable=True),
        sa.Column("taken_by_id", sa.String(length=64), nullable=True),
        sa.Column("taken_by_name", sa.String(length=255), nullable=True),
        sa.Column("taken_at", sa.DateTime(), nullable=True),
        sa.Column("checked_by_id", sa.String(length=64), nullable=True),
        sa.Column("checked_by_name", sa.String(length=255), nullable=True),
        sa.Column("checked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["ppr_event_id"], ["ppr_events.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ppr_event_id", "type", name="uq_event_notification_type"),
    )
    op.create_index("ix_ppr_notifications_ppr_event_id", "ppr_notifications", ["ppr_event_id"], unique=False)
    op.create_index("ix_ppr_notifications_scheduled_at", "ppr_notifications", ["scheduled_at"], unique=False)
    op.create_index("ix_ppr_notifications_status", "ppr_notifications", ["status"], unique=False)

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("notification_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=True),
        sa.Column("user_name", sa.String(length=255), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["notification_id"], ["ppr_notifications.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_log_notification_id", "audit_log", ["notification_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_audit_log_notification_id", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_ppr_notifications_status", table_name="ppr_notifications")
    op.drop_index("ix_ppr_notifications_scheduled_at", table_name="ppr_notifications")
    op.drop_index("ix_ppr_notifications_ppr_event_id", table_name="ppr_notifications")
    op.drop_table("ppr_notifications")
    op.drop_index("ix_ppr_events_external_id", table_name="ppr_events")
    op.drop_table("ppr_events")
