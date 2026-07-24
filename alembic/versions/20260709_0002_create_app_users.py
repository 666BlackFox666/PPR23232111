"""create app users

Revision ID: 20260709_0002
Revises: 20260708_0001
Create Date: 2026-07-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260709_0002"
down_revision: Union[str, Sequence[str], None] = "20260708_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "app_users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("telegram_id", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("full_name", sa.String(length=255), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_app_users_telegram_id", "app_users", ["telegram_id"], unique=True)
    op.create_index("ix_app_users_role", "app_users", ["role"], unique=False)
    op.create_index("ix_app_users_is_active", "app_users", ["is_active"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_app_users_is_active", table_name="app_users")
    op.drop_index("ix_app_users_role", table_name="app_users")
    op.drop_index("ix_app_users_telegram_id", table_name="app_users")
    op.drop_table("app_users")
