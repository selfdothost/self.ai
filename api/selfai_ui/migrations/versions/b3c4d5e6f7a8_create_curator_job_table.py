"""Create curator_job table

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-04-05 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

revision = "b3c4d5e6f7a8"
down_revision = "a2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade():
    print("Creating curator_job table")
    op.create_table(
        "curator_job",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("pipeline_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("priority", sa.Text(), nullable=False, server_default="'normal'"),
        sa.Column("scheduled_for", sa.BigInteger(), nullable=True),
        sa.Column("curator_job_id", sa.Text(), nullable=True),
        sa.Column("curator_url_idx", sa.BigInteger(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=True),
        # KB-to-Dataset finalization: a completed job creates a Knowledge
        # (dataset) record and links it back. Born on the table here rather
        # than bolted on by a later peewee ALTER (which broke on a fresh DB
        # and used Postgres-only `ADD COLUMN IF NOT EXISTS` syntax).
        sa.Column("dataset_name", sa.Text(), nullable=True),
        sa.Column("created_knowledge_id", sa.Text(), nullable=True),
    )


def downgrade():
    op.drop_table("curator_job")
