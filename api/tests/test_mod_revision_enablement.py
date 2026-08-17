"""The enablement gate every mod-owned revision shares (self.ai#85, P0).

`_enabled_mods()` used to be a byte-identical ~30-line copy inside each mod-owned
revision, because there was nowhere shared to put it. Two mods in, that was a
live hazard rather than an aesthetic one: the third copy would either be another
hand-copy or a naive `os.environ` read that rediscovers, in production, the two
failure modes the original docstring recorded -- a circular import when
`selfai_ui.config` is first imported mid-upgrade, and corruption of Alembic's
proxy bookkeeping surfacing as a `KeyError` in `_remove_proxy`.

It now lives once, in `migrations/util.py`, the module revisions already import
from. These tests pin the two things that make the collapse safe:

1. **The gate agrees with the running app's enablement view** -- the P0
   definition of done. A revision must decide "is my mod on?" the same way boot
   does, or a table gets created for a disabled mod (or skipped for an enabled
   one) and nothing reports it.
2. **The duplication does not come back.** A structural assertion over the
   revisions directory, because the failure this work removes is precisely
   "someone pasted the body again".
"""

import pathlib
import sys

import pytest

import selfai_ui
from selfai_ui.migrations.util import enabled_mods, mod_enabled

VERSIONS_DIR = pathlib.Path(selfai_ui.__file__).resolve().parent / "migrations" / "versions"


@pytest.mark.tier0
def test_the_gate_agrees_with_the_running_apps_enablement_view():
    # The P0 acceptance criterion. `selfai_ui.config` is imported by the time
    # tests run, so this exercises the PersistentConfig branch -- the one boot
    # uses once config is loaded.
    from selfai_ui import config as config_module

    assert enabled_mods() == list(config_module.ENABLED_MODS.value)


@pytest.mark.tier0
def test_the_persistent_config_value_wins_over_the_environment(monkeypatch):
    # A DB-persisted enablement override must be honoured. If the env var won
    # instead, enabling a mod through config would silently fail to create its
    # table on the next boot.
    from selfai_ui import config as config_module

    monkeypatch.setenv("ENABLED_MODS", "fromenv")

    # Assign/restore by hand rather than via monkeypatch.setattr: PersistentConfig
    # overrides __getattribute__ and raises TypeError on `__dict__`, so generic
    # attribute machinery is not safe to point at it. This is the same
    # save-assign-restore shape `mods_reference_boot.ensure_reference_table` uses.
    original = list(config_module.ENABLED_MODS.value)
    config_module.ENABLED_MODS.value = ["fromconfig"]
    try:
        assert enabled_mods() == ["fromconfig"]
        assert mod_enabled("fromconfig") is True
        assert mod_enabled("fromenv") is False
    finally:
        config_module.ENABLED_MODS.value = original


@pytest.mark.tier0
def test_the_environment_is_used_when_config_is_not_yet_imported(monkeypatch):
    # The window the helper exists for: an upgrade running before
    # `selfai_ui.config` has ever been imported (the test harness does exactly
    # this, and so does a fresh production boot). Consulting sys.modules rather
    # than importing is the whole point -- importing config here is what caused
    # the circular import and the proxy corruption.
    monkeypatch.delitem(sys.modules, "selfai_ui.config", raising=False)
    monkeypatch.setenv("ENABLED_MODS", " crew , reference ,, ")

    assert enabled_mods() == ["crew", "reference"]
    assert mod_enabled("crew") is True
    assert mod_enabled("nope") is False


@pytest.mark.tier0
def test_the_helper_never_imports_config_itself(monkeypatch):
    # Pins the mechanism, not just the result. If someone "simplifies" this to
    # `from selfai_ui import config`, both original failure modes return and
    # they only reproduce mid-upgrade, which is an expensive place to find them.
    monkeypatch.delitem(sys.modules, "selfai_ui.config", raising=False)
    monkeypatch.setenv("ENABLED_MODS", "crew")

    enabled_mods()

    assert "selfai_ui.config" not in sys.modules, (
        "enabled_mods() imported selfai_ui.config; it must only consult sys.modules"
    )


@pytest.mark.tier0
def test_an_unset_environment_enables_nothing(monkeypatch):
    monkeypatch.delitem(sys.modules, "selfai_ui.config", raising=False)
    monkeypatch.delenv("ENABLED_MODS", raising=False)

    assert enabled_mods() == []
    assert mod_enabled("crew") is False


# --- the duplication must not come back --------------------------------------

MOD_REVISIONS = sorted(VERSIONS_DIR.glob("*_add_mod_*.py"))


@pytest.mark.tier0
def test_the_mod_revisions_were_actually_found():
    # Guards the two assertions below against silently covering nothing.
    names = {path.name for path in MOD_REVISIONS}

    assert "b7e1c0ffee42_add_mod_reference_handles_table.py" in names
    assert "d2e3f4a5b6c7_add_mod_crew_sessions_table.py" in names


@pytest.mark.tier0
def test_no_revision_defines_its_own_enablement_helper():
    # The P0 definition of done: no `_enabled_mods()` body remains in
    # migrations/versions/. Checked across EVERY revision, not just the two that
    # had it, so a new mod revision pasting the body fails here.
    offenders = [
        path.name for path in VERSIONS_DIR.glob("*.py") if "def _enabled_mods" in path.read_text()
    ]

    assert not offenders, (
        f"revision(s) redefining the enablement helper: {offenders}. "
        f"Import `mod_enabled` from selfai_ui.migrations.util instead -- it carries "
        f"the circular-import and Alembic-proxy knowledge that a fresh copy will not."
    )


@pytest.mark.tier0
@pytest.mark.parametrize("path", MOD_REVISIONS, ids=lambda p: p.name)
def test_every_mod_revision_gates_through_the_shared_helper(path):
    body = path.read_text()

    assert "from selfai_ui.migrations.util import mod_enabled" in body, (
        f"{path.name} does not import the shared enablement helper"
    )
    assert "mod_enabled(" in body, f"{path.name} does not call mod_enabled()"


@pytest.mark.tier0
@pytest.mark.parametrize("path", MOD_REVISIONS, ids=lambda p: p.name)
def test_no_mod_revision_reads_the_enablement_env_var_directly(path):
    # The naive shortcut the shared helper exists to prevent. A revision reading
    # ENABLED_MODS straight from os.environ would ignore a DB-persisted override
    # and appear to work everywhere except the case that matters.
    body = path.read_text()

    assert 'environ.get("ENABLED_MODS"' not in body and "environ['ENABLED_MODS'" not in body, (
        f"{path.name} reads ENABLED_MODS from the environment directly; use mod_enabled()"
    )
