"""Pruning `alembic_version` rows that resolve to nothing (self.ai#85 P1 slice 1).

Verified on self.ai#86 against the pinned Alembic: a stamped revision whose file
is gone makes `upgrade heads` raise `Can't locate revision identified by
'<rev>'`; branch targeting does NOT contain it, because Alembic resolves the
whole revision map before doing anything; and core's own migrations are blocked
too, so the entire tenant's schema freezes rather than just the removed mod's.
Under the strict boot from #82 that is a refusal to serve -- so without pruning,
`rm -rf` on a mod directory would be a tenant outage recoverable only by
hand-editing `alembic_version` in production.

This lands BEFORE mods can own revisions, so it is inert on arrival: with only
core's directory configured every stamp resolves and the pruner finds nothing.
`test_pruning_is_inert_against_the_real_migrated_database` pins that, and is the
assertion that makes this safe to ship ahead of the rest of P1.

The interesting cases therefore need a throwaway Alembic environment with a
second location, because the shipped one cannot yet produce an orphan. These
build one and drive the real runner over it rather than mocking the resolver --
the behaviour under test is Alembic's, and a mocked version of it would prove
nothing.
"""

import pathlib
import tempfile

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine

import selfai_ui.config as config_module
from selfai_ui.config import _alembic_config, _prune_orphaned_mod_stamps, _stamped_revisions

ENV_PY = (
    "from alembic import context\n"
    "from sqlalchemy import create_engine\n"
    "url = context.config.get_main_option('sqlalchemy.url')\n"
    "engine = create_engine(url)\n"
    "with engine.connect() as connection:\n"
    "    context.configure(connection=connection, target_metadata=None)\n"
    "    with context.begin_transaction():\n"
    "        context.run_migrations()\n"
)


def _revision(where: pathlib.Path, rev: str, down, branch, table: str) -> None:
    (where / f"{rev}.py").write_text(
        f"revision = {rev!r}\ndown_revision = {down!r}\n"
        f"branch_labels = {branch!r}\ndepends_on = None\n"
        "from alembic import op\nimport sqlalchemy as sa\n"
        f"def upgrade():\n    op.create_table({table!r}, sa.Column('id', sa.Integer, primary_key=True))\n"
        f"def downgrade():\n    op.drop_table({table!r})\n"
    )


@pytest.fixture
def two_location_env(monkeypatch):
    """A core lineage plus a separate mod lineage, both applied.

    Mirrors the shape P1 produces: the mod is an independent base with its own
    branch label, so `alembic_version` carries a row per branch.
    """
    root = pathlib.Path(tempfile.mkdtemp())
    core = root / "core_versions"
    mod = root / "mods" / "alpha" / "migrations" / "versions"
    core.mkdir(parents=True)
    mod.mkdir(parents=True)
    db = root / "t.db"
    url = f"sqlite:///{db}"

    (root / "env.py").write_text(ENV_PY)
    (root / "alembic.ini").write_text(f"[alembic]\nscript_location = {root}\nsqlalchemy.url = {url}\n")

    _revision(core, "core1", None, ("core",), "core_one")
    _revision(mod, "alpha1", None, ("mod_alpha",), "mod_alpha_thing")

    def build():
        cfg = AlembicConfig(str(root / "alembic.ini"))
        cfg.set_main_option("version_locations", f"{core} {mod}")
        return cfg

    command.upgrade(build(), "heads")

    # The pruner reads the module-level DATABASE_URL, the same one the running
    # app uses; point it at this throwaway database for the duration.
    monkeypatch.setattr(config_module, "DATABASE_URL", url)

    return {"root": root, "core": core, "mod": mod, "url": url, "build": build}


@pytest.mark.tier0
def test_pruning_is_inert_against_the_real_migrated_database():
    # THE assertion that makes this safe to ship before the rest of P1. Against
    # the shipped configuration -- one location, every stamp resolvable -- the
    # pruner must find nothing and delete nothing.
    before = _stamped_revisions(create_engine(config_module.DATABASE_URL))

    pruned = _prune_orphaned_mod_stamps(_alembic_config())

    after = _stamped_revisions(create_engine(config_module.DATABASE_URL))
    assert pruned == []
    assert after == before


@pytest.mark.tier0
def test_an_orphaned_mod_stamp_is_pruned(two_location_env):
    env = two_location_env
    engine = create_engine(env["url"])
    assert sorted(_stamped_revisions(engine)) == ["alpha1", "core1"]

    # Uninstall the mod: its directory, and therefore its revision, is gone.
    (env["mod"] / "alpha1.py").unlink()
    cfg = AlembicConfig(str(env["root"] / "alembic.ini"))
    cfg.set_main_option("version_locations", str(env["core"]))

    assert _prune_orphaned_mod_stamps(cfg) == ["alpha1"]
    assert _stamped_revisions(create_engine(env["url"])) == ["core1"]


@pytest.mark.tier0
def test_upgrade_works_again_after_pruning(two_location_env):
    # The point of the whole exercise: an uninstalled mod must not freeze core.
    env = two_location_env
    (env["mod"] / "alpha1.py").unlink()
    cfg = AlembicConfig(str(env["root"] / "alembic.ini"))
    cfg.set_main_option("version_locations", str(env["core"]))

    with pytest.raises(Exception, match="Can't locate revision"):
        command.upgrade(cfg, "heads")

    _prune_orphaned_mod_stamps(cfg)

    # And core can move forward again, which is the failure #86 measured: an
    # unresolvable stamp blocks core's own migrations, not just the mod's.
    _revision(env["core"], "core2", "core1", None, "core_two")
    command.upgrade(cfg, "heads")

    assert _stamped_revisions(create_engine(env["url"])) == ["core2"]


@pytest.mark.tier0
def test_a_still_installed_mod_is_not_pruned(two_location_env):
    # Disabling a mod must never orphan it. P1 discovers by INSTALLATION and
    # gates by ENABLEMENT precisely so this case cannot arise; pinned here
    # because getting it wrong turns a routine disable into an outage.
    env = two_location_env

    assert _prune_orphaned_mod_stamps(env["build"]()) == []
    assert sorted(_stamped_revisions(create_engine(env["url"]))) == ["alpha1", "core1"]


@pytest.mark.tier0
def test_pruning_reports_at_error_level(two_location_env, caplog):
    # Deleting a row from alembic_version is not routine. The boot succeeds, so
    # the log line is the only trace an operator gets.
    import logging

    env = two_location_env
    (env["mod"] / "alpha1.py").unlink()
    cfg = AlembicConfig(str(env["root"] / "alembic.ini"))
    cfg.set_main_option("version_locations", str(env["core"]))

    with caplog.at_level(logging.ERROR, logger=config_module.log.name):
        _prune_orphaned_mod_stamps(cfg)

    records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert records, "pruning was silent"
    assert "alpha1" in records[0].getMessage()


@pytest.mark.tier0
def test_a_never_migrated_database_is_handled(tmp_path, monkeypatch):
    # No alembic_version table at all -- a fresh volume. Must be a quiet no-op,
    # not an error: this runs on every boot including the very first.
    empty = tmp_path / "fresh.db"
    empty.touch()
    monkeypatch.setattr(config_module, "DATABASE_URL", f"sqlite:///{empty}")

    assert _stamped_revisions(create_engine(f"sqlite:///{empty}")) == []
    assert _prune_orphaned_mod_stamps(_alembic_config()) == []
