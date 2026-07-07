"""Create job_window and job_window_slot tables

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-04-05 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

revision = "c4d5e6f7a8b9"
down_revision = "b3c4d5e6f7a8"
branch_labels = None
depends_on = None


def upgrade():
    print("Creating job_window table")
    op.create_table(
        "job_window",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("start_at", sa.BigInteger(), nullable=False),
        sa.Column("end_at", sa.BigInteger(), nullable=False),
        sa.Column("preferred_job_type", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=True),
    )

    print("Creating job_window_slot table")
    op.create_table(
        "job_window_slot",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("window_id", sa.Text(), nullable=False),
        sa.Column("job_type", sa.Text(), nullable=False),
        sa.Column("max_concurrent", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("min_remaining_minutes", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade():
    op.drop_table("job_window_slot")
    op.drop_table("job_window")
