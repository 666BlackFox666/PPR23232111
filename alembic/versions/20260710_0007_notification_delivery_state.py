"""notification delivery state

Revision ID: 20260710_0007
Revises: 20260710_0006
Create Date: 2026-07-10 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260710_0007"
down_revision: Union[str, Sequence[str], None] = "20260710_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ppr_notifications", sa.Column("sent_at", sa.DateTime(), nullable=True))
    op.add_column("ppr_notifications", sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("ppr_notifications", sa.Column("last_attempt_at", sa.DateTime(), nullable=True))
    op.add_column("ppr_notifications", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column("ppr_notifications", sa.Column("processing_started_at", sa.DateTime(), nullable=True))
    op.add_column("ppr_notifications", sa.Column("processing_by", sa.String(length=128), nullable=True))
    op.add_column("ppr_notifications", sa.Column("telegram_edit_last_error", sa.Text(), nullable=True))
    op.add_column("ppr_notifications", sa.Column("reminder_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("ppr_notifications", sa.Column("last_reminder_at", sa.DateTime(), nullable=True))
    op.create_index("ix_ppr_notifications_processing_by", "ppr_notifications", ["processing_by"], unique=False)
    op.alter_column("ppr_notifications", "attempt_count", server_default=None)
    op.alter_column("ppr_notifications", "reminder_count", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_ppr_notifications_processing_by", table_name="ppr_notifications")
    op.drop_column("ppr_notifications", "last_reminder_at")
    op.drop_column("ppr_notifications", "reminder_count")
    op.drop_column("ppr_notifications", "telegram_edit_last_error")
    op.drop_column("ppr_notifications", "processing_by")
    op.drop_column("ppr_notifications", "processing_started_at")
    op.drop_column("ppr_notifications", "last_error")
    op.drop_column("ppr_notifications", "last_attempt_at")
    op.drop_column("ppr_notifications", "attempt_count")
    op.drop_column("ppr_notifications", "sent_at")
