"""dashboard filter indexes

Revision ID: 20260710_0006
Revises: 20260710_0005
Create Date: 2026-07-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "20260710_0006"
down_revision: Union[str, Sequence[str], None] = "20260710_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_ppr_events_date", "ppr_events", ["date"], unique=False)
    op.create_index("ix_ppr_events_project", "ppr_events", ["project"], unique=False)
    op.create_index("ix_ppr_notifications_taken_by_id", "ppr_notifications", ["taken_by_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_ppr_notifications_taken_by_id", table_name="ppr_notifications")
    op.drop_index("ix_ppr_events_project", table_name="ppr_events")
    op.drop_index("ix_ppr_events_date", table_name="ppr_events")
