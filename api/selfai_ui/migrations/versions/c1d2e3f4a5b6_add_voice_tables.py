"""Add voice and voice_file tables (Sound Studio — Voices Workspace)

Revision ID: c1d2e3f4a5b6
Revises: c2d3e4f5a6b7
Create Date: 2026-07-29 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "c1d2e3f4a5b6"
# Re-chained onto c2d3e4f5a6b7 (the vram-consumer-reservation migration that
# merged to main concurrently) so there is a single linear head, not a branch.
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade():
    print("Creating voice table")
    op.create_table(
        "voice",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        # The node-graph pipeline that builds the voice — a structured field.
        sa.Column("graph", sa.JSON(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("access_control", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.BigInteger(), nullable=True),
    )

    print("Creating voice_file table")
    op.create_table(
        "voice_file",
        sa.Column(
            "voice_id",
            sa.Text(),
            sa.ForeignKey("voice.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "file_id",
            sa.String(),
            sa.ForeignKey("file.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("created_at", sa.BigInteger(), nullable=True),
    )
    op.create_index("ix_voice_file_file_id", "voice_file", ["file_id"])


def downgrade():
    op.drop_index("ix_voice_file_file_id", table_name="voice_file")
    op.drop_table("voice_file")
    op.drop_table("voice")
