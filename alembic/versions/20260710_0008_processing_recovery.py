"""processing recovery and scheduler heartbeat

Revision ID: 20260710_0008
Revises: 20260710_0007
Create Date: 2026-07-10 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260710_0008"
down_revision: Union[str, Sequence[str], None] = "20260710_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ppr_notifications", sa.Column("processing_phase", sa.String(length=32), nullable=True))
    op.create_table(
        "scheduler_heartbeats",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("last_poll_at", sa.DateTime(), nullable=True),
        sa.Column("last_successful_poll_at", sa.DateTime(), nullable=True),
        sa.Column("last_poll_error", sa.Text(), nullable=True),
        sa.Column("is_running", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scheduler_heartbeats_worker_id", "scheduler_heartbeats", ["worker_id"], unique=True)
    op.create_index("ix_scheduler_heartbeats_is_running", "scheduler_heartbeats", ["is_running"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_scheduler_heartbeats_is_running", table_name="scheduler_heartbeats")
    op.drop_index("ix_scheduler_heartbeats_worker_id", table_name="scheduler_heartbeats")
    op.drop_table("scheduler_heartbeats")
    op.drop_column("ppr_notifications", "processing_phase")
