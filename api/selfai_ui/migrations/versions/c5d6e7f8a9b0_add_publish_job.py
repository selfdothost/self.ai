"""Add publish_job table

self.ai#131 R6 — a publish merges a line's accumulated adapters into its base
and produces a new base GGUF. Its own job kind, never a side effect of a bake,
so it gets its own record: what base it started from, which adapters it
intended to merge, and which version it produced.

`adapter_version_ids` is written at creation rather than derived at completion
on purpose. A publish that fails should still be able to say what it was trying
to merge; recomputing "adapters since the current base" after the fact would
give a different answer once the line moves on.

Deliberately SQLite-safe: create_table only, no alter_column and no
drop_column.

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-08-12

Head was verified directly rather than inferred from a green pipeline. From
!438's CI: an Alembic CYCLE fails loudly (conftest import and the strict boot
check both catch it), but a FORK into two heads does NOT — every job passes and
it surfaces only as a CrashLoop on the next roll (#82, #102).

"""

import sqlalchemy as sa
from alembic import op

revision = "c5d6e7f8a9b0"
down_revision = "b4c5d6e7f8a9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "publish_job",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("line_id", sa.Text(), nullable=True),
        sa.Column("user_id", sa.Text(), nullable=True),
        sa.Column("base_version_id", sa.Text(), nullable=True),
        sa.Column("adapter_version_ids", sa.JSON(), nullable=True),
        sa.Column("output_name", sa.Text(), nullable=True),
        sa.Column("quant_type", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("priority", sa.Text(), nullable=True),
        sa.Column("scheduled_for", sa.BigInteger(), nullable=True),
        sa.Column("llamolotl_job_id", sa.Text(), nullable=True),
        sa.Column("llamolotl_url_idx", sa.BigInteger(), nullable=True),
        sa.Column("result_version_id", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.BigInteger(), nullable=True),
    )
    op.create_index("ix_publish_job_line_id", "publish_job", ["line_id"])


def downgrade():
    op.drop_index("ix_publish_job_line_id", table_name="publish_job")
    op.drop_table("publish_job")
