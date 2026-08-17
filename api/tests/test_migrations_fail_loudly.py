"""Boot migrations must fail the boot, not the operator's next afternoon (self.ai#82).

`run_migrations()` used to wrap `command.upgrade(cfg, "head")` in a bare
`except Exception as e: print(f"Error: {e}")`. Nothing downstream checked the
outcome, so a failed migration left a pod Running, Ready, and serving against a
schema that was absent or half-built. The only trace was one line on stdout,
which nothing monitors.

The concrete production consequence is the reason these tests exist rather than
a comment: `benchmark_config` never got created, so
`BenchmarkConfigs.get_by_benchmark()` returned None for every benchmark, so
`_fits_in_window()` took its `if not cfg: return True` branch, and every
GPU-window fit check passed unconditionally on the shared 4090 -- a safety gate
degraded to always-yes behind a green pod (self.chat#26).

Two guarantees are pinned here:

1. **A failed upgrade raises.** Not logged-and-continued, not printed. The
   process must not reach the point of serving.
2. **A returning upgrade is not trusted on its own.** `_assert_at_head` reads
   the revision back out of the database, because "upgrade returned" and "the
   schema is at head" are different facts -- an interrupted run, a gated
   revision, or a connection pointed at a different database all produce the
   first without the second.

The failure is injected at `alembic.command.upgrade` rather than by corrupting a
real database: the behaviour under test is this module's error handling, and a
test that had to break a schema to prove it would be testing Alembic.
"""

import logging

import pytest
from sqlalchemy import create_engine, text

import selfai_ui.config as config_module
from selfai_ui.config import MigrationError, _alembic_config, _assert_at_head, run_migrations


@pytest.mark.tier0
def test_a_failed_upgrade_raises_instead_of_being_swallowed(monkeypatch):
    # The regression this file exists for. Before the fix this call returned
    # normally and boot continued.
    def _explode(*args, **kwargs):
        raise RuntimeError("relation \"config\" already exists")

    monkeypatch.setattr("alembic.command.upgrade", _explode)

    with pytest.raises(MigrationError):
        run_migrations()


@pytest.mark.tier0
def test_the_raised_error_keeps_the_original_cause(monkeypatch):
    # The operator needs the underlying DB error, not just "migrations failed".
    original = RuntimeError("could not connect to server")

    def _explode(*args, **kwargs):
        raise original

    monkeypatch.setattr("alembic.command.upgrade", _explode)

    with pytest.raises(MigrationError) as excinfo:
        run_migrations()

    assert excinfo.value.__cause__ is original
    assert "could not connect to server" in str(excinfo.value)


@pytest.mark.tier0
def test_the_failure_goes_to_the_logger_not_stdout(monkeypatch, caplog):
    # `print()` was the original sin: it lands nowhere anyone watches. The
    # replacement must reach the logging tree, with a traceback attached.
    def _explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("alembic.command.upgrade", _explode)

    with caplog.at_level(logging.ERROR, logger=config_module.log.name):
        with pytest.raises(MigrationError):
            run_migrations()

    failures = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert failures, "the upgrade failure was not logged at ERROR or above"
    assert any(r.exc_info for r in failures), "no traceback attached to the failure record"


@pytest.mark.tier0
def test_a_database_not_at_head_is_refused(monkeypatch, tmp_path):
    # An empty database has no alembic_version row at all, so its current
    # revision is None -- the shape a pod gets when migrations never ran. It
    # must be refused rather than served.
    empty_db = tmp_path / "not-migrated.db"
    empty_db.touch()
    monkeypatch.setattr(config_module, "DATABASE_URL", f"sqlite:///{empty_db}")

    with pytest.raises(MigrationError) as excinfo:
        _assert_at_head(_alembic_config())

    assert "not at head" in str(excinfo.value)


@pytest.mark.tier0
def test_a_stale_revision_is_refused(monkeypatch, tmp_path):
    # Worse than empty and likelier: a database that IS stamped, but at a
    # revision that is no longer head. `upgrade` returning would not catch it.
    stale_db = tmp_path / "stale.db"
    engine = create_engine(f"sqlite:///{stale_db}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('7e5b5dc7342b')"))
    engine.dispose()

    monkeypatch.setattr(config_module, "DATABASE_URL", f"sqlite:///{stale_db}")

    with pytest.raises(MigrationError) as excinfo:
        _assert_at_head(_alembic_config())

    # Names both what it found and what it wanted, so the fix is obvious from
    # the crash rather than requiring an `alembic current` round trip.
    assert "7e5b5dc7342b" in str(excinfo.value)


@pytest.mark.tier0
def test_the_migrated_test_database_passes_the_head_check():
    # The positive case, and it is not a tautology: conftest runs a real
    # `upgrade head` against this database before importing config, so this
    # asserts the check agrees with a genuinely migrated schema rather than
    # with itself. If a new revision is added without this passing, the head
    # set and the stamped revision have diverged.
    _assert_at_head(_alembic_config())


@pytest.mark.tier0
def test_run_migrations_is_still_invoked_at_import():
    # The guarantee is worthless if the call site is ever removed: the schema
    # gate only holds because importing config performs it.
    source = (config_module.__file__ or "").strip()
    assert source, "could not locate the config module source"

    with open(source, encoding="utf-8") as handle:
        body = handle.read()

    assert "\nrun_migrations()\n" in body, "run_migrations() is no longer called at import of config"
