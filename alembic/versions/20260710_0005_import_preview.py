"""import preview and source keys

Revision ID: 20260710_0005
Revises: 20260710_0004
Create Date: 2026-07-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260710_0005"
down_revision: Union[str, Sequence[str], None] = "20260710_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ppr_events", sa.Column("source_key", sa.String(length=255), nullable=True))

    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE ppr_events
            SET source_key = CASE
                WHEN external_id LIKE 'MANUAL-%' THEN NULL
                WHEN external_id LIKE 'ROW-%' THEN 'row:' || substring(external_id from 5)
                ELSE 'id:' || external_id
            END
            WHERE source_key IS NULL
            """
        )
    )

    op.create_index("ix_ppr_events_source_key", "ppr_events", ["source_key"], unique=True)

    op.create_table(
        "import_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("preview_id", sa.String(length=64), nullable=True),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("started_by_id", sa.String(length=64), nullable=True),
        sa.Column("started_by_name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("file_hash", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_import_runs_preview_id", "import_runs", ["preview_id"], unique=True)
    op.create_index("ix_import_runs_file_hash", "import_runs", ["file_hash"], unique=False)
    op.create_index("ix_import_runs_mode", "import_runs", ["mode"], unique=False)
    op.create_index("ix_import_runs_status", "import_runs", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_import_runs_status", table_name="import_runs")
    op.drop_index("ix_import_runs_mode", table_name="import_runs")
    op.drop_index("ix_import_runs_file_hash", table_name="import_runs")
    op.drop_index("ix_import_runs_preview_id", table_name="import_runs")
    op.drop_table("import_runs")

    op.drop_index("ix_ppr_events_source_key", table_name="ppr_events")
    op.drop_column("ppr_events", "source_key")
