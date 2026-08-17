"""Add `kind` to chat — which surface owns a conversation

Phase 2 of the Tokenization Studio programme (self.ai#134 line of work; plan:
self.chat `context/plans/build-site-tokenization-shell.md`, finding A and X-3).

A tokenization session must not appear in the chat sidebar, and an ordinary
chat must not appear in the tokenization history. `chat.meta` is a JSON column
and would have been the obvious home, but every list filter in
`models/chats.py` is a real COLUMN (`folder_id`, `archived`, `pinned`) because
those lists are paginated server-side:

* filtering on a JSON key needs a dialect-specific expression, and PostgreSQL
  and SQLite differ — the same reason the `3f7eff4c5814` rekey was told to
  read/modify/write blobs in Python rather than use a JSON operator;
* filtering in Python after the query silently breaks `skip`/`limit`,
  returning short pages rather than an error.

So: a column, following `folder_id`'s precedent. Named as a KIND rather than a
boolean so the next surface that needs its own history does not add another
column.

NULLABLE WITH NO SERVER DEFAULT, and that is the point. NULL means ordinary
chat, so every existing row is already correct and there is no backfill — the
`UPDATE` this avoids would rewrite every chat row on a live database for no
behavioural gain. New chat rows keep writing NULL because
`insert_new_chat(kind=None)` is the default.

Additive only: no alter_column, no drop_column, no constraint on existing
data, so this replays on a fresh SQLite file as well as on Postgres
(self.ai#94 makes SQLite a supported mode). `ALTER TABLE ... ADD COLUMN` is one
of the few DDL forms SQLite has always supported.

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-08-12

CHAIN, and RE-POINTED. Written against `b4c5d6e7f8a9` (the #131 model-versioning
tables) and originally numbered `c5d6e7f8a9b0` -- which turned out to be the
exact id a concurrent branch had already used for `add_publish_job`, also
revising `b4c5d6e7f8a9`. Two revisions sharing an id is not a fork Alembic can
report; it is a duplicate key. Renumbered to `d6e7f8a9b0c1` and re-pointed onto
the publish revision, so the chain stays linear:

    d7e8f9a0b1c2 -> 3f7eff4c5814 -> b4c5d6e7f8a9 -> c5d6e7f8a9b0 -> d6e7f8a9b0c1

Heeding the warning `b4c5d6e7f8a9`'s docstring records from !438's CI: a CYCLE
fails loudly, but a FORK into two heads passes every job and only surfaces as a
CrashLoop at boot, because `_assert_at_head` checks membership of
`get_heads()`. The head count was therefore verified directly after writing
this, not inferred from a green pipeline.
"""

import sqlalchemy as sa
from alembic import op

revision = "d6e7f8a9b0c1"
down_revision = "c5d6e7f8a9b0"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chat", sa.Column("kind", sa.Text(), nullable=True))


def downgrade():
    # A real inverse, not a `pass`. Dropping it is safe in both directions
    # because NULL is the ordinary-chat value: any row this loses was a
    # non-chat session, and the surface that owns those is gated off in the
    # same release. SQLite has supported DROP COLUMN since 3.35 (self.ai#94).
    op.drop_column("chat", "kind")
