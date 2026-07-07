"""Add training_course and training_job tables

Revision ID: a1b2c3d4e5f6
Revises: 4ace53fd72c8
Create Date: 2026-03-17 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "a1b2c3d4e5f6"
down_revision = "3781e22d8b01"
branch_labels = None
depends_on = None


def upgrade():
    print("Creating training_course table")
    op.create_table(
        "training_course",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("data", sa.JSON(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("access_control", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=True),
    )

    print("Creating training_job table")
    op.create_table(
        "training_job",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("course_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("llamolotl_job_id", sa.Text(), nullable=True),
        sa.Column("llamolotl_url_idx", sa.BigInteger(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=True),
    )


def downgrade():
    op.drop_table("training_job")
    op.drop_table("training_course")
