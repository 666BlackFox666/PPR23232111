"""manual ppr administration fields

Revision ID: 20260709_0003
Revises: 20260709_0002
Create Date: 2026-07-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260709_0003"
down_revision: Union[str, Sequence[str], None] = "20260709_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ppr_events", sa.Column("is_manually_edited", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("ppr_events", sa.Column("manual_updated_at", sa.DateTime(), nullable=True))
    op.add_column("ppr_notifications", sa.Column("auto_send_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.create_index("ix_ppr_notifications_auto_send_enabled", "ppr_notifications", ["auto_send_enabled"], unique=False)

    with op.batch_alter_table("audit_log") as batch_op:
        batch_op.alter_column("notification_id", existing_type=sa.Integer(), nullable=True)
        batch_op.add_column(sa.Column("ppr_event_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_audit_log_ppr_event_id_ppr_events", "ppr_events", ["ppr_event_id"], ["id"])
        batch_op.create_index("ix_audit_log_ppr_event_id", ["ppr_event_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("audit_log") as batch_op:
        batch_op.drop_index("ix_audit_log_ppr_event_id")
        batch_op.drop_constraint("fk_audit_log_ppr_event_id_ppr_events", type_="foreignkey")
        batch_op.drop_column("ppr_event_id")
        batch_op.alter_column("notification_id", existing_type=sa.Integer(), nullable=False)

    op.drop_index("ix_ppr_notifications_auto_send_enabled", table_name="ppr_notifications")
    op.drop_column("ppr_notifications", "auto_send_enabled")
    op.drop_column("ppr_events", "manual_updated_at")
    op.drop_column("ppr_events", "is_manually_edited")
