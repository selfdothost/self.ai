"""Add scheduled_for column to training_job and eval_job

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-03-23 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "training_job",
        sa.Column("scheduled_for", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "eval_job",
        sa.Column("scheduled_for", sa.BigInteger(), nullable=True),
    )


def downgrade():
    op.drop_column("eval_job", "scheduled_for")
    op.drop_column("training_job", "scheduled_for")
