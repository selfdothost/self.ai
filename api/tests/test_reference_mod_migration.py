"""R5: the reference mod's per-mod Alembic migration, proven end to end.

These tests drive the REAL Alembic runner (`command.upgrade(cfg, "head")`)
against a throwaway SQLite DB -- a full upgrade from base through every revision,
including `b7e1c0ffee42_add_mod_reference_handles_table.py`. They observe the
table's presence/absence in the resulting database, never by inspecting the
revision file's source (cavekit-mods-reference-implementation R5, criterion 4:
"Running the normal upgrade with the mod enabled creates the table; the table is
absent when the mod is not enabled").

The migration reads enablement from `selfai_ui.config.ENABLED_MODS.value` and the
table name from `selfai_ui.mods.naming.table_prefix_for("reference")`. The env
module (`migrations/env.py`) forces `sqlalchemy.url` from
`selfai_ui.env.DATABASE_URL`, so each test points that at its own temp DB file --
never the shared conftest test DB -- so nothing here pollutes other tests.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

import selfai_ui.env
from selfai_ui.config import ENABLED_MODS
from selfai_ui.mods.naming import table_prefix_for

_API_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_INI = _API_ROOT / "selfai_ui" / "alembic.ini"
_MIGRATIONS = _API_ROOT / "selfai_ui" / "migrations"

EXPECTED_TABLE = f"{table_prefix_for('reference')}handles"


def _run_upgrade_to_head(db_path: Path) -> None:
    """Run the real Alembic upgrade from base to head against a fresh DB file."""
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")


def _apply_enablement(monkeypatch, db_path: Path, enabled_mods: list[str]) -> None:
    """Point the runner at `db_path` and set ENABLED_MODS for this upgrade.

    env.py reads `selfai_ui.env.DATABASE_URL` fresh each invocation, so patching
    it here redirects the whole run to the throwaway DB. The migration reads
    `ENABLED_MODS.value`; setting it drives the enablement gate.
    """
    monkeypatch.setattr(selfai_ui.env, "DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setattr(ENABLED_MODS, "value", enabled_mods)


def test_expected_table_name_is_mod_reference_prefixed():
    """The table the migration creates begins with `mod_reference_` (R5-3)."""
    assert EXPECTED_TABLE == "mod_reference_handles"
    assert EXPECTED_TABLE.startswith("mod_reference_")


def test_upgrade_with_reference_enabled_creates_the_table(tmp_path, monkeypatch):
    """Enabled: a normal upgrade to head creates `mod_reference_handles`."""
    db_path = tmp_path / "enabled.db"
    _apply_enablement(monkeypatch, db_path, ["reference"])

    _run_upgrade_to_head(db_path)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        tables = set(inspect(engine).get_table_names())
        assert (
            EXPECTED_TABLE in tables
        ), f"{EXPECTED_TABLE} missing after upgrade with the mod enabled: {sorted(tables)}"

        # The table is genuinely usable and carries the {task_id, status} shape.
        columns = {c["name"] for c in inspect(engine).get_columns(EXPECTED_TABLE)}
        assert {"task_id", "status"} <= columns, columns
        with engine.begin() as conn:
            conn.execute(
                text(f"INSERT INTO {EXPECTED_TABLE} (task_id, status, created_at) VALUES (:t, :s, :c)"),
                {"t": "task-1", "s": "pending", "c": 0},
            )
            row = conn.execute(text(f"SELECT task_id, status FROM {EXPECTED_TABLE}")).fetchone()
        assert tuple(row) == ("task-1", "pending")
    finally:
        engine.dispose()


def test_upgrade_with_reference_disabled_omits_the_table(tmp_path, monkeypatch):
    """Disabled: the same upgrade to head leaves no `mod_reference_` table."""
    db_path = tmp_path / "disabled.db"
    _apply_enablement(monkeypatch, db_path, [])

    _run_upgrade_to_head(db_path)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        tables = set(inspect(engine).get_table_names())
        assert EXPECTED_TABLE not in tables, f"{EXPECTED_TABLE} was created even though the mod is not enabled"
        # No orphan mod_reference_* table of any name.
        assert not [t for t in tables if t.startswith("mod_reference_")], sorted(tables)
        # But the upgrade DID reach head -- core tables are present.
        assert "alembic_version" in tables and "config" in tables, sorted(tables)
    finally:
        engine.dispose()


def test_migration_creates_no_unprefixed_and_alters_no_core_table(tmp_path, monkeypatch):
    """R5-6: enabling adds exactly the one prefixed table over the disabled run."""
    disabled_db = tmp_path / "d.db"
    enabled_db = tmp_path / "e.db"

    _apply_enablement(monkeypatch, disabled_db, [])
    _run_upgrade_to_head(disabled_db)
    _apply_enablement(monkeypatch, enabled_db, ["reference"])
    _run_upgrade_to_head(enabled_db)

    d_engine = create_engine(f"sqlite:///{disabled_db}")
    e_engine = create_engine(f"sqlite:///{enabled_db}")
    try:
        disabled_tables = set(inspect(d_engine).get_table_names())
        enabled_tables = set(inspect(e_engine).get_table_names())
    finally:
        d_engine.dispose()
        e_engine.dispose()

    added = enabled_tables - disabled_tables
    assert added == {EXPECTED_TABLE}, f"Enabling should add only {EXPECTED_TABLE}; added={sorted(added)}"
    # No core table removed or renamed by the mod's revision.
    assert not (disabled_tables - enabled_tables)
