"""Add the reference mod's one table: mod_reference_handles

Revision ID: b7e1c0ffee42
Revises: d5e6f7a8b9c0
Create Date: 2026-07-23

The first real per-mod Alembic migration in this codebase
(cavekit-mods-reference-implementation R5). It creates the single table the
`reference` mod owns, under the prefix `table_prefix_for("reference")` derives --
`mod_reference_`.

LOCATION DECISION (recorded here because this is the first migration of its kind).
This revision lives in the main `api/selfai_ui/migrations/versions/` directory
alongside the other 25 revisions -- NOT inside `api/mods/reference/`. The reason,
determined by reading the runner rather than guessing:

  * `api/selfai_ui/alembic.ini` configures no `version_locations` -- the setting
    exists only as a commented example (line 46), so the default single location
    `migrations/versions` is the only place revisions are discovered.
  * `api/selfai_ui/migrations/env.py` does no dynamic scanning of
    `api/mods/*/migrations/`; it is the stock Alembic env module.
  * The pinned Alembic is 1.18.5 (`requirements-api.txt`). Even though modern
    Alembic supports multiple version locations, nothing in this repo configures
    it, so a revision under `api/mods/reference/` would simply never be found.
  * Making the runner discover mod-owned directories would be a change to the
    Alembic runner itself, which this kit's Out of Scope explicitly forbids
    ("Changes to the Alembic runner itself"; cavekit-mods-overview.md project-wide
    Out of Scope). Building that discovery mechanism is unauthorized new work.

So the revision is mod-owned *in substance*, not by file location:
  1. its table carries the `mod_reference_` prefix `table_prefix_for("reference")`
     derives (used here, not hardcoded);
  2. both `upgrade()` and `downgrade()` are gated on the `reference` mod being
     enabled (`"reference" in ENABLED_MODS.value`), so an instance that never
     enabled the mod gets no orphan table; and
  3. this docstring names it as the reference mod's migration.

A migration file is not mod source code -- it lives under
`api/selfai_ui/migrations/versions/`, outside `MODS_DIR = API_ROOT / "mods"` that
`test_mods_facade_boundary.py` scans -- so it MAY import `selfai_ui` internals
(`selfai_ui.config`, `selfai_ui.mods.naming`) directly. It is not subject to the
facade-boundary rule that binds `api/mods/` source.

The table mirrors the reference mod's async-handle shape ({task_id, status};
see `api/mods/reference/state.py`'s `ReferenceState.record`). R5 requires only
that the table exist, be correctly prefixed, and be enablement-gated -- it does
not require the mod's runtime code to read or write it in this task.
"""

import logging

import sqlalchemy as sa
from alembic import op

from selfai_ui.migrations.util import mod_enabled

revision = "b7e1c0ffee42"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None

log = logging.getLogger(__name__)


def _reference_enabled() -> bool:
    """True when the `reference` mod is in the enabled list."""
    return mod_enabled("reference")


def _table_name() -> str:
    """The reference mod's one table name, prefix derived (not hardcoded)."""
    from selfai_ui.mods.naming import table_prefix_for

    return f"{table_prefix_for('reference')}handles"


def upgrade():
    if not _reference_enabled():
        log.info("reference mod not enabled; skipping creation of its %shandles table", "mod_reference_")
        return

    op.create_table(
        _table_name(),
        # Mirrors the reference mod's {task_id, status} async-handle shape.
        sa.Column("task_id", sa.String(), primary_key=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.BigInteger()),
    )


def downgrade():
    if not _reference_enabled():
        return

    op.drop_table(_table_name())
