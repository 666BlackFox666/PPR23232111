"""split ppr and notification statuses

Revision ID: 20260710_0004
Revises: 20260709_0003
Create Date: 2026-07-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260710_0004"
down_revision: Union[str, Sequence[str], None] = "20260709_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ppr_events", sa.Column("ppr_status", sa.String(length=32), nullable=False, server_default="scheduled"))
    op.create_index("ix_ppr_events_ppr_status", "ppr_events", ["ppr_status"], unique=False)

    connection = op.get_bind()
    connection.execute(sa.text("UPDATE ppr_events SET ppr_status = 'archived' WHERE is_active = false"))
    connection.execute(
        sa.text(
            """
            UPDATE ppr_events
            SET ppr_status = 'verified'
            WHERE is_active = true
              AND id IN (
                SELECT ppr_event_id
                FROM ppr_notifications
                WHERE status = 'checked'
              )
            """
        )
    )
    connection.execute(
        sa.text(
            """
            UPDATE ppr_events
            SET ppr_status = 'in_progress'
            WHERE is_active = true
              AND ppr_status = 'scheduled'
              AND id IN (
                SELECT ppr_event_id
                FROM ppr_notifications
                WHERE status = 'in_progress'
              )
            """
        )
    )
    connection.execute(sa.text("UPDATE ppr_notifications SET status = 'sent' WHERE status IN ('checked', 'in_progress')"))
    connection.execute(sa.text("UPDATE ppr_notifications SET status = 'failed' WHERE status = 'error'"))


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE ppr_notifications
            SET status = 'checked'
            WHERE ppr_event_id IN (
                SELECT id FROM ppr_events WHERE ppr_status = 'verified'
            )
            """
        )
    )
    connection.execute(
        sa.text(
            """
            UPDATE ppr_notifications
            SET status = 'in_progress'
            WHERE ppr_event_id IN (
                SELECT id FROM ppr_events WHERE ppr_status = 'in_progress'
            )
            """
        )
    )
    connection.execute(sa.text("UPDATE ppr_notifications SET status = 'error' WHERE status = 'failed'"))

    op.drop_index("ix_ppr_events_ppr_status", table_name="ppr_events")
    op.drop_column("ppr_events", "ppr_status")
