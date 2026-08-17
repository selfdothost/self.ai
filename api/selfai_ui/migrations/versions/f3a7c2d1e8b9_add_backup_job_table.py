"""Add backup_job table

self.ai#93 — a real backup/restore path. Records each backup or restore run,
the scopes it covered, where the archive landed in self.corpus, and the Alembic
revision it was taken at.

Deliberately SQLite-safe: create_table only, no drop_column and no
alter_column, so this replays on a fresh SQLite file as well as Postgres
(self.ai#94 makes SQLite a supported mode).

Revision ID: f3a7c2d1e8b9
Revises: f2a3b4c5d6e7
Create Date: 2026-08-01

Originally written against e1f2a3b4c5d6. Re-pointed at f2a3b4c5d6e7 (the eval
catalog's custom_eval table, self.ai#91) when that landed on main first — both
had the same parent, which would have left the chain with two heads. Boot
migrations are strict (#82) and `_assert_at_head` checks membership of
`get_heads()`, so a branched chain is a CrashLoop waiting for whichever pod
rolls next, not a merge inconvenience.

"""

import sqlalchemy as sa
from alembic import op

revision = "f3a7c2d1e8b9"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "backup_job",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=True),
        sa.Column("archive_repo", sa.Text(), nullable=True),
        sa.Column("archive_path", sa.Text(), nullable=True),
        sa.Column("archive_commit", sa.Text(), nullable=True),
        sa.Column("archive_bytes", sa.BigInteger(), nullable=True),
        sa.Column("schema_revision", sa.Text(), nullable=True),
        sa.Column("progress", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.BigInteger(), nullable=True),
    )


def downgrade():
    op.drop_table("backup_job")
