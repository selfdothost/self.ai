"""The migration chain must replay end to end on SQLite (self.ai#94).

`tests/conftest.py` already points `DATABASE_URL` at a file-backed SQLite
database and runs `alembic upgrade head` before any test imports, so the chain
is in fact exercised on SQLite on every single test run. That made it an
*implicit* guarantee nobody was asserting — these tests make it explicit, so a
new revision that only works on Postgres fails here rather than surfacing the
first time someone builds the demo or CI image.

The historical concern was `op.drop_column`: SQLite could not drop a column
before 3.35 (2021), and Alembic prefers `batch_alter_table` for SQLite. On a
modern SQLite runtime the plain `drop_column` sites replay natively, which is
why this passes today. The version floor is asserted below so that fact stays
visible rather than being rediscovered.
"""

import sqlite3

import pytest
from sqlalchemy import inspect

pytestmark = pytest.mark.tier0

# SQLite gained ALTER TABLE ... DROP COLUMN in 3.35.0. Below that the chain's
# 16 op.drop_column sites cannot replay without batch_alter_table wrappers.
MINIMUM_SQLITE = (3, 35, 0)


def _version_tuple(text: str) -> tuple:
    return tuple(int(part) for part in text.split(".")[:3])


def test_sqlite_runtime_is_new_enough_to_drop_columns():
    assert _version_tuple(sqlite3.sqlite_version) >= MINIMUM_SQLITE, (
        f"SQLite {sqlite3.sqlite_version} predates ALTER TABLE DROP COLUMN "
        f"({'.'.join(str(p) for p in MINIMUM_SQLITE)}); the migration chain's "
        "op.drop_column sites cannot replay on it."
    )


def test_database_under_test_is_sqlite():
    """Guards the premise of everything else in this module."""
    from selfai_ui.internal.db import engine

    assert engine.dialect.name == "sqlite", (
        f"expected the test database to be SQLite, got {engine.dialect.name!r} — "
        "this module asserts the chain replays on SQLite specifically"
    )


def test_chain_replayed_to_head_on_sqlite():
    """conftest ran `alembic upgrade head` against SQLite. Read the answer back
    out of the database rather than trusting that it returned — the same posture
    `config._assert_at_head` takes at boot (#82)."""
    from alembic.runtime.migration import MigrationContext

    from selfai_ui.internal.db import engine
    from selfai_ui.utils.backup import script_heads

    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()

    assert current is not None, "no alembic revision stamped on the SQLite database"
    assert current in script_heads(), f"SQLite database is at {current!r}, not at a script head"


def test_core_tables_exist_after_the_chain():
    """A chain can report success while leaving tables uncreated — the
    `benchmark_config` failure behind #82 was exactly that shape."""
    from selfai_ui.internal.db import engine

    names = set(inspect(engine).get_table_names())
    expected = {"user", "auth", "chat", "prompt", "knowledge", "file", "backup_job"}
    missing = expected - names
    assert not missing, f"tables missing after upgrade head on SQLite: {sorted(missing)}"
