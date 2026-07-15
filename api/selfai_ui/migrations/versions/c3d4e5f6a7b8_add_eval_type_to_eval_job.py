"""Add eval_type column to eval_job table

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-03-20 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "eval_job",
        sa.Column("eval_type", sa.Text(), nullable=True, server_default="code-eval"),
    )
    # Backfill existing rows
    op.execute("UPDATE eval_job SET eval_type = 'code-eval' WHERE eval_type IS NULL")


def downgrade():
    op.drop_column("eval_job", "eval_type")
