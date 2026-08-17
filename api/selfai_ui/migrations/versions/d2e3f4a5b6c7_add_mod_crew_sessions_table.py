"""Add the crew mod's session table: mod_crew_sessions

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-07-29

The second per-mod migration in this codebase, and the first for a mod authored
outside this repo (self.crew's `crew` mod, self.crew#141 / self.crew!193). It
creates the single table that mod owns, under the prefix
`table_prefix_for("crew")` derives -- `mod_crew_`.

WHY THIS LIVES HERE AND NOT IN self.crew
----------------------------------------
For the same reason `b7e1c0ffee42_add_mod_reference_handles_table.py` records at
length, and that reasoning is not repeated here: `alembic.ini` configures no
`version_locations` and `migrations/env.py` does no scanning, so revisions are
discovered from exactly one directory -- this one. Teaching the runner to find
mod-owned directories is a change to the Alembic runner, which is out of scope
project-wide.

self.crew reached the same conclusion by reading that revision, and deliberately
did not invent a discovery mechanism or hand-create the table. This file is the
answer to that ask.

The table is therefore mod-owned *in substance* rather than by location:

  1. its name carries the `mod_crew_` prefix `table_prefix_for("crew")` derives
     (used here, never hardcoded -- a hardcoded name is how a prefix rename
     silently orphans a table);
  2. both `upgrade()` and `downgrade()` are gated on the `crew` mod being enabled
     (`"crew" in ENABLED_MODS.value`), so an instance that never enabled it gets
     no orphan table; and
  3. this docstring names it as the crew mod's migration.

A migration file is not mod source code -- it lives outside
`MODS_DIR = API_ROOT / "mods"`, which is what `test_mods_facade_boundary.py`
scans -- so it MAY import `selfai_ui` internals directly. It is not bound by the
facade rule that governs `api/mods/` source.

THE ROW SHAPE
-------------
Taken from self.crew's `_DDL` in `mods/crew/tests/test_persistence.py`, which
pins this schema and drives their sqlite-backed suite. Their accessors and this
table are meant to fail *their* tests rather than production if the two ever
drift, so the column set matches theirs exactly, in their order.

Two notes on the translation from their sqlite DDL to something both dialects
accept -- prod is Postgres (`DATABASE_URL=postgresql...`), their tests are
sqlite:

  * `guard_tripped BOOLEAN NOT NULL DEFAULT 0` is expressed as `sa.Boolean` with
    `sa.false()` as the server default. A literal `DEFAULT 0` is valid sqlite and
    an error on Postgres ("column is of type boolean but default expression is of
    type integer"); `sa.false()` renders per-dialect and is `0` on one and `false`
    on the other. Their store binds `bool(...)` in both directions, so a real
    boolean column is what it already expects.
  * `task_ids` and `drivers` hold JSON lists as TEXT, matching their store, which
    does its own `json.dumps`/`loads`. They are not `sa.JSON`: making them a JSON
    column here would change what their `SELECT` returns (a decoded object rather
    than a string) and break the round-trip their tests pin.

No indexes. `for_owner` and `for_captain` filter in SQL, but this table holds one
row per live crew session -- tens, not thousands -- so an index would be cost
without a reader. Add one when a query is actually slow, not in advance.
"""

import logging

import sqlalchemy as sa
from alembic import op

from selfai_ui.migrations.util import mod_enabled

revision = "d2e3f4a5b6c7"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None

log = logging.getLogger(__name__)


def _crew_enabled() -> bool:
    """True when the `crew` mod is in the enabled list."""
    return mod_enabled("crew")


def _table_name() -> str:
    """The crew mod's one table name, prefix derived (not hardcoded)."""
    from selfai_ui.mods.naming import table_prefix_for

    return f"{table_prefix_for('crew')}sessions"


def upgrade():
    if not _crew_enabled():
        log.info("crew mod not enabled; skipping creation of its %ssessions table", "mod_crew_")
        return

    op.create_table(
        _table_name(),
        # The session id crew-code mints at attach. Not a core id -- cross-domain
        # relations are by id only, so there is no FK to a core table here.
        sa.Column("session_id", sa.Text(), primary_key=True),
        # SessionFlow: how this row came to exist (remote_attach, ready_room).
        sa.Column("flow", sa.Text(), nullable=False),
        # The captain this session wraps. Deliberately nullable and empty for
        # remote-attach rows, which have no captain (self.crew Gap 6).
        sa.Column("captain", sa.Text()),
        # The user the session is bound to. The authorization record itself:
        # the CR annotation is not consulted (self.crew kit R6).
        sa.Column("owner_user_id", sa.Text(), nullable=False),
        # CaptainPhase, PascalCase, verbatim from the controller's vocabulary.
        # Partitioned live/held/terminal on the mod side; stored as written.
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("guard_tripped", sa.Boolean(), nullable=False, server_default=sa.false()),
        # JSON list, encoded by the mod. Text, not sa.JSON -- see the docstring.
        sa.Column("task_ids", sa.Text()),
        sa.Column("namespace", sa.Text()),
        sa.Column("created", sa.Text()),
        sa.Column("updated", sa.Text()),
        # JSON list of admirals authorized to drive a Ready Room. Cleared on a
        # terminal-phase rebind, which is why it is a column and not derived.
        sa.Column("drivers", sa.Text()),
        # Boot-reconciliation tri-state (confirmed / stale / unknown), or NULL
        # for a row the controller has not been asked about yet.
        sa.Column("reconcile", sa.Text()),
    )


def downgrade():
    if not _crew_enabled():
        return

    op.drop_table(_table_name())
