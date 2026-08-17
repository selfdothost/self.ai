"""`b4c5d6e7f8a9` — model_line / model_version, proven end to end (self.ai#131).

Modelled on `test_crew_mod_migration.py`: drives the REAL Alembic runner
(`command.upgrade(cfg, "head")`) against a throwaway SQLite DB and observes the
resulting database, rather than reading the revision's source.

The load-bearing assertion is `test_existing_model_row_is_untouched`. This
revision is meant to be purely additive — a `model` row that belongs to no line
must keep serving exactly as it does today — and the only way to show that is
to seed one before the upgrade and read it back after.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

import selfai_ui.env

_API_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_INI = _API_ROOT / "selfai_ui" / "alembic.ini"
_MIGRATIONS = _API_ROOT / "selfai_ui" / "migrations"

_REVISION = "b4c5d6e7f8a9"
#: Downgrading TO the parent is what "undo this revision" means. Named rather
#: than `-1` so a later revision landing in between cannot silently retarget it.
_REVISION_PARENT = "d7e8f9a0b1c2"

EXPECTED_LINE_COLUMNS = [
    "id",
    "name",
    "user_id",
    "corpus_repo",
    "current_version_id",
    "access_control",
    "meta",
    "created_at",
    "updated_at",
]

EXPECTED_VERSION_COLUMNS = [
    "id",
    "line_id",
    "sequence",
    "kind",
    "corpus_commit_id",
    "parent_version_id",
    "base_version_id",
    "produced_by",
    "published_by",
    "artifact_ref",
    "meta",
    "created_at",
]


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _point_runner_at(monkeypatch, db_path: Path) -> None:
    """env.py reads `selfai_ui.env.DATABASE_URL` fresh each invocation."""
    monkeypatch.setattr(selfai_ui.env, "DATABASE_URL", f"sqlite:///{db_path}")


def test_upgrade_creates_both_tables(tmp_path, monkeypatch):
    db_path = tmp_path / "upgrade.db"
    _point_runner_at(monkeypatch, db_path)
    command.upgrade(_cfg(db_path), "head")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert {"model_line", "model_version"} <= tables


def test_columns_match_the_table_definitions(tmp_path, monkeypatch):
    """Written out literally, not derived from the model module.

    Deriving them would make this test agree with whatever the migration says,
    which is the drift it exists to catch.
    """
    db_path = tmp_path / "columns.db"
    _point_runner_at(monkeypatch, db_path)
    command.upgrade(_cfg(db_path), "head")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        line_columns = inspect(engine).get_columns("model_line")
        version_columns = inspect(engine).get_columns("model_version")
    finally:
        engine.dispose()

    assert [c["name"] for c in line_columns] == EXPECTED_LINE_COLUMNS
    assert [c["name"] for c in version_columns] == EXPECTED_VERSION_COLUMNS


def test_existing_model_row_is_untouched(tmp_path, monkeypatch):
    """The additive promise: a model with no line keeps serving as it does now."""
    db_path = tmp_path / "additive.db"
    _point_runner_at(monkeypatch, db_path)

    command.upgrade(_cfg(db_path), _REVISION_PARENT)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            before = [c["name"] for c in inspect(conn).get_columns("model")]
            conn.execute(
                text(
                    "INSERT INTO model (id, user_id, name, params, meta, is_active,"
                    " created_at, updated_at)"
                    " VALUES ('m1', 'u1', 'gemma', '{}', '{}', 1, 1, 1)"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(_cfg(db_path), "head")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            after = [c["name"] for c in inspect(conn).get_columns("model")]
            row = conn.execute(text("SELECT id, name, is_active FROM model WHERE id = 'm1'")).fetchone()
    finally:
        engine.dispose()

    assert after == before, "the model table gained or lost a column"
    assert row is not None, "the pre-existing model row did not survive the upgrade"
    assert row[1] == "gemma"


def test_downgrade_removes_both_tables_and_upgrade_replays(tmp_path, monkeypatch):
    db_path = tmp_path / "roundtrip.db"
    _point_runner_at(monkeypatch, db_path)
    cfg = _cfg(db_path)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, _REVISION_PARENT)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert not ({"model_line", "model_version"} & tables), "downgrade left a table behind"

    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert {"model_line", "model_version"} <= tables


def test_revision_is_reachable_from_head(tmp_path, monkeypatch):
    """A branched chain is a CrashLoop on the next roll (#82/#102)."""
    from alembic.script import ScriptDirectory

    cfg = _cfg(tmp_path / "heads.db")
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()

    assert len(heads) == 1, f"expected a single head, found {heads}"
    revisions = {rev.revision for rev in script.walk_revisions("base", heads[0])}
    assert _REVISION in revisions
