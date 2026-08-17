"""The crew mod's per-mod Alembic migration, proven end to end (self.crew#141).

Modelled on `test_reference_mod_migration.py`, which does the same job for
`b7e1c0ffee42`. These drive the REAL Alembic runner (`command.upgrade(cfg,
"head")`) against a throwaway SQLite DB -- a full upgrade from base through every
revision, including `d2e3f4a5b6c7_add_mod_crew_sessions_table.py` -- and observe
the table in the resulting database rather than by reading the revision's source.

WHY THE COLUMN ASSERTION IS THE POINT OF THIS FILE.
The crew mod lives in another repo. Its store issues positional `SELECT`s over a
fixed column tuple and its suite pins the schema as `_DDL` in
`mods/crew/tests/test_persistence.py`, so their tests fail if their accessors
drift from that DDL. Nothing over there can fail if *this* table drifts from it
instead -- self.crew has no copy of this file and cannot run this migration. The
exact column set below is the only place the two repos are held together, which
is why it is asserted as equality against a literal list rather than as a subset.

`EXPECTED_COLUMNS` is written out in their DDL's order and not derived from
anything, deliberately: deriving it from the migration would make this test agree
with whatever the migration says, which is precisely the failure it exists to
catch.
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

EXPECTED_TABLE = f"{table_prefix_for('crew')}sessions"

#: The crew revision, and the revision immediately before it. Downgrading TO the
#: parent is what "undo the crew revision" means; see
#: test_downgrade_removes_the_table for why this is named rather than `-1`.
_CREW_REVISION = "d2e3f4a5b6c7"
_CREW_REVISION_PARENT = "c1d2e3f4a5b6"

#: self.crew's pinned `_DDL`, transcribed. Order is theirs; the store's
#: `_COLUMNS` tuple selects positionally in this order.
EXPECTED_COLUMNS = [
    "session_id",
    "flow",
    "captain",
    "owner_user_id",
    "phase",
    "guard_tripped",
    "task_ids",
    "namespace",
    "created",
    "updated",
    "drivers",
    "reconcile",
]


def _run_upgrade_to_head(db_path: Path) -> None:
    """Run the real Alembic upgrade from base to head against a fresh DB file."""
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")


def _apply_enablement(monkeypatch, db_path: Path, enabled_mods: list[str]) -> None:
    """Point the runner at `db_path` and set ENABLED_MODS for this upgrade.

    env.py reads `selfai_ui.env.DATABASE_URL` fresh each invocation, so patching
    it here redirects the whole run to the throwaway DB -- never the shared
    conftest test DB. The migration reads `ENABLED_MODS.value`; setting it drives
    the enablement gate. Note this enables the mod *by name only*: the migration
    never imports mod code, which is what lets this run in a repo where the crew
    mod is not installed.
    """
    monkeypatch.setattr(selfai_ui.env, "DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setattr(ENABLED_MODS, "value", enabled_mods)


def test_expected_table_name_is_mod_crew_prefixed():
    """The table the migration creates begins with `mod_crew_`, and is the exact
    name self.crew's `SESSIONS_TABLE` composes (`TABLE_PREFIX + "sessions"`)."""
    assert EXPECTED_TABLE == "mod_crew_sessions"
    assert EXPECTED_TABLE.startswith("mod_crew_")


def test_upgrade_with_crew_enabled_creates_the_table(tmp_path, monkeypatch):
    """Enabled: a normal upgrade to head creates `mod_crew_sessions`."""
    db_path = tmp_path / "enabled.db"
    _apply_enablement(monkeypatch, db_path, ["crew"])

    _run_upgrade_to_head(db_path)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        tables = set(inspect(engine).get_table_names())
        assert (
            EXPECTED_TABLE in tables
        ), f"{EXPECTED_TABLE} missing after upgrade with the mod enabled: {sorted(tables)}"
    finally:
        engine.dispose()


def test_columns_match_self_crews_pinned_ddl_exactly(tmp_path, monkeypatch):
    """The column set is theirs, exactly -- no extra, none missing, same order.

    An extra column is as much a defect as a missing one: their store writes an
    explicit column list on INSERT, so a NOT NULL column they do not know about
    would fail every write, and any other extra is a schema they did not agree to.
    """
    db_path = tmp_path / "columns.db"
    _apply_enablement(monkeypatch, db_path, ["crew"])
    _run_upgrade_to_head(db_path)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        columns = inspect(engine).get_columns(EXPECTED_TABLE)
    finally:
        engine.dispose()

    assert [c["name"] for c in columns] == EXPECTED_COLUMNS

    by_name = {c["name"]: c for c in columns}
    # The three the DDL constrains. `captain` is deliberately nullable -- it is
    # empty for remote-attach rows (self.crew Gap 6) -- so a NOT NULL here would
    # break the flow that works today.
    assert by_name["flow"]["nullable"] is False
    assert by_name["owner_user_id"]["nullable"] is False
    assert by_name["phase"]["nullable"] is False
    assert by_name["captain"]["nullable"] is True
    assert by_name["session_id"]["primary_key"]


def test_table_round_trips_a_row_the_crew_store_would_write(tmp_path, monkeypatch):
    """A real INSERT/SELECT in the shape their store issues.

    Covers the two translations from their sqlite DDL that could plausibly be got
    wrong: `guard_tripped` as a genuine boolean with a server default (they bind
    `bool(...)`, and a Postgres `DEFAULT 0` on a boolean column is an error), and
    the JSON-list columns as TEXT rather than `sa.JSON` (a JSON column would hand
    their `SELECT` a decoded object where their code calls `json.loads`).
    """
    db_path = tmp_path / "roundtrip.db"
    _apply_enablement(monkeypatch, db_path, ["crew"])
    _run_upgrade_to_head(db_path)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            # Omitting guard_tripped exercises the server default -- their store
            # relies on the column being NOT NULL with a false default.
            conn.execute(
                text(
                    f"INSERT INTO {EXPECTED_TABLE} "
                    "(session_id, flow, captain, owner_user_id, phase, task_ids, "
                    " namespace, created, updated, drivers, reconcile) "
                    "VALUES (:sid, :flow, :cap, :uid, :phase, :tasks, :ns, :cre, :upd, :drv, :rec)"
                ),
                {
                    "sid": "sess-1",
                    "flow": "remote_attach",
                    "cap": "",  # empty, not NULL: a remote-attach row has no captain
                    "uid": "user-1",
                    "phase": "Running",
                    "tasks": '["task-a"]',
                    "ns": "crew-system",
                    "cre": "2026-07-29T00:00:00Z",
                    "upd": "2026-07-29T00:00:00Z",
                    "drv": '["admiral-1"]',
                    "rec": None,
                },
            )
            row = conn.execute(
                text(
                    f"SELECT session_id, flow, captain, owner_user_id, phase, guard_tripped, "
                    f"task_ids, drivers, reconcile FROM {EXPECTED_TABLE} WHERE session_id = :sid"
                ),
                {"sid": "sess-1"},
            ).fetchone()
    finally:
        engine.dispose()

    assert row is not None
    assert row.session_id == "sess-1"
    assert row.captain == ""
    assert bool(row.guard_tripped) is False  # the server default landed
    assert row.task_ids == '["task-a"]'  # still text, not a decoded list
    assert row.drivers == '["admiral-1"]'
    assert row.reconcile is None  # tri-state not yet set by boot reconciliation


def test_upgrade_with_crew_disabled_omits_the_table(tmp_path, monkeypatch):
    """Disabled: the same upgrade to head leaves no `mod_crew_` table."""
    db_path = tmp_path / "disabled.db"
    _apply_enablement(monkeypatch, db_path, [])

    _run_upgrade_to_head(db_path)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        tables = set(inspect(engine).get_table_names())
        assert EXPECTED_TABLE not in tables, f"{EXPECTED_TABLE} was created even though the mod is not enabled"
        assert not [t for t in tables if t.startswith("mod_crew_")], sorted(tables)
        # But the upgrade DID reach head -- core tables are present.
        assert "alembic_version" in tables and "config" in tables, sorted(tables)
    finally:
        engine.dispose()


def test_migration_creates_no_unprefixed_and_alters_no_core_table(tmp_path, monkeypatch):
    """Enabling adds exactly the one prefixed table over the disabled run."""
    disabled_db = tmp_path / "d.db"
    enabled_db = tmp_path / "e.db"

    _apply_enablement(monkeypatch, disabled_db, [])
    _run_upgrade_to_head(disabled_db)
    _apply_enablement(monkeypatch, enabled_db, ["crew"])
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


def test_the_pinned_revision_ids_match_the_migration():
    """Guard the constants above against drift.

    Hardcoding a revision id trades one rot risk for another, so this makes the
    new one loud: if the crew revision is ever renumbered or re-parented, this
    fails with the mismatch instead of the downgrade test failing with a
    confusing "table survived" message.
    """
    import re

    source = next(_MIGRATIONS.glob(f"versions/{_CREW_REVISION}_*.py")).read_text()
    parent = re.search(r'^down_revision\s*=\s*["\']([^"\']+)', source, re.M)
    assert parent and parent.group(1) == _CREW_REVISION_PARENT, (
        f"_CREW_REVISION_PARENT is {_CREW_REVISION_PARENT} but the migration says "
        f"{parent.group(1) if parent else None}"
    )


def test_downgrade_removes_the_table(tmp_path, monkeypatch):
    """The revision is reversible: downgrading past it drops the table.

    Worth proving rather than assuming -- a `downgrade()` that raises would strand
    an operator mid-rollback, and this is the only revision on this chain whose
    downgrade is conditional.

    Targets this revision's parent explicitly rather than `-1` from head. `-1`
    silently meant "undo the crew revision" only for as long as the crew revision
    happened to BE head; the moment any unrelated revision landed on top, `-1`
    undid that one instead and this test failed claiming the crew table had
    "survived a downgrade" when nothing had tried to remove it. Naming the target
    says what the test means and cannot rot.
    """
    db_path = tmp_path / "downgrade.db"
    _apply_enablement(monkeypatch, db_path, ["crew"])
    _run_upgrade_to_head(db_path)

    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.downgrade(cfg, _CREW_REVISION_PARENT)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert EXPECTED_TABLE not in tables, f"{EXPECTED_TABLE} survived a downgrade: {sorted(tables)}"
    assert "config" in tables, "the downgrade took a core table with it"
