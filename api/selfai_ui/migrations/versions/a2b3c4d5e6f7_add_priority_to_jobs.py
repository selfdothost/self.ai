"""Add priority column to eval_job and training_job

Revision ID: a2b3c4d5e6f7
Revises: f6a7b8c9d0e1
Create Date: 2026-04-05 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

revision = "a2b3c4d5e6f7"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade():
    print("Adding priority column to eval_job")
    op.add_column(
        "eval_job",
        sa.Column("priority", sa.Text(), nullable=False, server_default="'normal'"),
    )

    print("Adding priority column to training_job")
    op.add_column(
        "training_job",
        sa.Column("priority", sa.Text(), nullable=False, server_default="'normal'"),
    )


def downgrade():
    op.drop_column("eval_job", "priority")
    op.drop_column("training_job", "priority")
