"""Add model_line and model_version tables

self.ai#131 — model versions as first-class, commit-addressable objects. A
line is the durable identity ("the model"); a version is one point on it,
backed by a self.corpus commit, either a base GGUF or an adapter.

Purely additive. **No column is added to, and no constraint placed on, the
existing `model` table**: a model that belongs to no line must keep serving
exactly as it does today, which is what makes this safe to roll ahead of any
of the code that reads these tables.

`sequence` carries ordering explicitly rather than leaving it to `created_at`.
Two versions written in the same second still need a defined order, and a
history that reshuffles under a clock adjustment is not a history.

Deliberately SQLite-safe: create_table only, no alter_column and no
drop_column, so this replays on a fresh SQLite file as well as on Postgres
(self.ai#94 makes SQLite a supported mode).

Revision ID: b4c5d6e7f8a9
Revises: 3f7eff4c5814
Create Date: 2026-08-12

RE-POINTED. Written against head d7e8f9a0b1c2, but the permission-rekey
migration from `build-site-studio-rename-permissions.md` T-002 targeted that
same parent and landed first, as `3f7eff4c5814` (self.ai#134, !438, merged
f75b0400). Per the note this docstring already carried, the second of the two
re-points at the other rather than at d7e8f9a0b1c2 — so this now revises
3f7eff4c5814 and the chain stays linear:

    d7e8f9a0b1c2 -> 3f7eff4c5814 -> b4c5d6e7f8a9

Boot migrations are strict (#82) and `_assert_at_head` checks membership of
`get_heads()`, so a branched chain is a CrashLoop on the next roll rather than
a merge inconvenience (#102).

Worth recording from !438's own CI, because it changes how much green is worth
here: a CYCLE is caught loudly (it failed both test:api at conftest import and
pages:validate at the strict boot check). A FORK into two heads is NOT — every
job passes and it only surfaces at boot. Verify the head count directly rather
than trusting a green pipeline.

"""

import sqlalchemy as sa
from alembic import op

revision = "b4c5d6e7f8a9"
down_revision = "3f7eff4c5814"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "model_line",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("user_id", sa.Text(), nullable=True),
        sa.Column("corpus_repo", sa.Text(), nullable=True),
        sa.Column("current_version_id", sa.Text(), nullable=True),
        sa.Column("access_control", sa.JSON(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.BigInteger(), nullable=True),
    )
    op.create_table(
        "model_version",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("line_id", sa.Text(), nullable=True),
        sa.Column("sequence", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=True),
        sa.Column("corpus_commit_id", sa.Text(), nullable=True),
        sa.Column("parent_version_id", sa.Text(), nullable=True),
        sa.Column("base_version_id", sa.Text(), nullable=True),
        sa.Column("produced_by", sa.JSON(), nullable=True),
        sa.Column("published_by", sa.Text(), nullable=True),
        sa.Column("artifact_ref", sa.Text(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=True),
    )
    op.create_index("ix_model_version_line_id", "model_version", ["line_id"])


def downgrade():
    op.drop_index("ix_model_version_line_id", table_name="model_version")
    op.drop_table("model_version")
    op.drop_table("model_line")
