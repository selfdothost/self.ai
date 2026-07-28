"""R5 at the REAL application-boot level: the migration-created table is usable
through the running app, and the migration's enablement gate is driven by the
running app's live config (cavekit-mods-reference-implementation R5, criteria
3/4/6 -- the app-boot half).

WHAT T-007 ALREADY PROVES (and this file deliberately does NOT re-prove).
`tests/test_reference_mod_migration.py` drives the Alembic runner directly --
`command.upgrade(cfg, "head")` against throwaway SQLite DBs -- and observes, in
the resulting databases, that enabling `reference` creates exactly
`mod_reference_handles` (R5-3, R5-4), that disabling omits it and every
`mod_reference_*` table (R5-4), and that enabling adds only that one table over
the disabled run while touching no core table (R5-6). That is the
migration-runner-level proof of the gate. It is complete and this file does not
duplicate it.

WHAT T-014 ADDS. The project's Verification Convention (cavekit-mods-overview.md)
holds that a criterion is only satisfied "at boot" by starting the REAL
`selfai_ui.main.app` the deployed process serves -- not a standalone equivalent.
T-007 proves the gate against a throwaway engine; it never touches the running
app. This file supplies that missing half through `reference_booted_client`:

  * Enabled boot -> the table the migration created is genuinely QUERYABLE and
    WRITABLE from within the running app, through the exact `get_db` a mod uses
    (the `selfai_ui.modapi` facade export a mod is confined to), with a real
    INSERT then SELECT round trip while the app is live. This reaches R5-4
    ("creates the table") and R5-6 (a usable table) all the way to the running
    app's own DB access -- not merely an inspector on a throwaway engine, and
    not merely "present" (which is all T-009's fixture needs).
  * The migration's OWN enablement gate (`b7e1c0ffee42._reference_enabled`),
    evaluated against each boot's LIVE config, is on under the reference-enabled
    boot and off under the plain boot. That is the app-boot-level meaning of
    "gated on enablement": the gate reads the same
    `selfai_ui.config.ENABLED_MODS.value` the running app carries, so the gate's
    decision tracks the deployed app's real enablement state -- not a value a
    unit test monkeypatched onto a bare runner.

A REAL FINDING ABOUT "ABSENT WHEN NOT ENABLED" AT THE APP-BOOT DB LEVEL.
The application boot path never runs Alembic: `TestClient(app)`'s lifespan runs
`boot_mods`, not `command.upgrade`. So no single app boot creates or omits
`mod_reference_handles`; the table's presence in the shared, file-backed test DB
is decided entirely by whatever ran the migration -- conftest's session-start
`upgrade head` with `reference` disabled (which omits it), or
`ensure_reference_table` inside `reference_booted_client` (which creates it).
Once ANY `reference_booted_client` test has run in a session, that helper has
created the table in the shared DB and it persists for the rest of the session.
Therefore "the table is absent from a disabled boot's DB view" cannot be
re-proven cleanly at the app-boot level within a shared-session run -- such an
assertion would depend on test collection order. The disabled->absent property
IS proven, against fresh throwaway DBs, by T-007
(`test_upgrade_with_reference_disabled_omits_the_table`). At the app-boot level
the honest, order-independent residual is the gate-reads-live-config property
below, paired with the plain boot genuinely not loading the mod (no route, gate
off) -- which is what this file proves rather than forcing a flaky DB-absence
assertion to pass.
"""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text

from selfai_ui import config as config_module
from selfai_ui.mods.naming import table_prefix_for

# `reference_booted_client` is provided by the tests.mods_reference_boot plugin
# (registered in tests/conftest.py); request it by name, do not import it. Only
# the mod-id constant is imported.
from tests.mods_reference_boot import REFERENCE_MOD_ID

# tests/routers/ -> tests/ -> api/
_API_ROOT = Path(__file__).resolve().parent.parent.parent
_MIGRATION_FILE = (
    _API_ROOT / "selfai_ui" / "migrations" / "versions" / "b7e1c0ffee42_add_mod_reference_handles_table.py"
)

#: Derived, not hardcoded, so this test agrees with the migration's own authority
#: on the table name (`table_prefix_for("reference")` + "handles").
EXPECTED_TABLE = f"{table_prefix_for(REFERENCE_MOD_ID)}handles"


def _load_migration_module():
    """Load the reference mod's revision file as a standalone module so its OWN
    enablement gate (`_reference_enabled`) can be evaluated against the running
    app's live config.

    Loading it by path with a private name does NOT register it in Alembic's
    version graph -- it is just the module object -- so it is inert with respect
    to the migration story. This exercises the migration's real gate function,
    not a re-implementation of it, which is the point: what runs under the real
    upgrade is exactly this predicate.
    """
    spec = importlib.util.spec_from_file_location("_reference_migration_under_test", _MIGRATION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.tier1
def test_enabled_boot_table_is_usable_through_the_apps_own_db_access(reference_booted_client):
    """Enabled boot: the migration-created table is usable through the running
    app's own DB access (the facade `get_db` a mod is confined to), and the
    migration's gate reads the running app's live config as ON.

    This is the app-boot reach of R5-4/R5-6 that T-007's throwaway-engine test
    cannot make: T-007 proves the table exists in a fresh DB after the runner
    runs; this proves the table the migration created is reachable and writable
    from inside the deployed application object, through the same `get_db`
    export the mod's source may import.
    """
    boot = reference_booted_client

    # Precondition: the REAL running app enabled and loaded the reference mod...
    assert REFERENCE_MOD_ID in boot.app.state.MODS.loaded, boot.app.state.MODS.errors
    # ...and the app's live enablement config carries it.
    assert REFERENCE_MOD_ID in config_module.ENABLED_MODS.value

    # The migration's own gate, evaluated against that LIVE config, is ON -- so
    # the real upgrade under this boot's config would create the table (R5:
    # "gated on enablement", read from the running app's config, not a bare
    # monkeypatched runner).
    migration = _load_migration_module()
    assert migration._reference_enabled() is True
    # And the gate agrees with this file on which table the upgrade creates.
    assert migration._table_name() == EXPECTED_TABLE

    # The table is usable through the exact `get_db` a mod uses -- the facade
    # export -- with a real INSERT then SELECT round trip while the app is live.
    from selfai_ui.modapi import get_db

    task_id = "t014-boot-usable"
    try:
        with get_db() as db:
            db.execute(
                text(f"INSERT INTO {EXPECTED_TABLE} (task_id, status, created_at) VALUES (:t, :s, :c)"),
                {"t": task_id, "s": "submitted", "c": 0},
            )
            db.commit()

        with get_db() as db:
            row = db.execute(
                text(f"SELECT task_id, status FROM {EXPECTED_TABLE} WHERE task_id = :t"),
                {"t": task_id},
            ).fetchone()

        assert row is not None, f"{EXPECTED_TABLE} was not usable through the running app's get_db"
        assert tuple(row) == (task_id, "submitted")
    finally:
        # Leave the shared, non-truncated table as we found it (conftest's
        # truncation order does not include mod_reference_handles).
        with get_db() as db:
            db.execute(text(f"DELETE FROM {EXPECTED_TABLE} WHERE task_id = :t"), {"t": task_id})
            db.commit()


@pytest.mark.tier1
def test_plain_boot_does_not_enable_the_mod_and_the_migration_gate_is_off(client):
    """Plain boot (reference NOT in ENABLED_MODS): the running app does not enable
    or load the mod, and the migration's gate reads this boot's live config as
    OFF -- the app-boot-level, order-INDEPENDENT meaning of "gated on enablement"
    for the disabled side.

    Deliberately NOT asserted here, because both are order-DEPENDENT artifacts of
    the shared singleton app + shared file-backed DB (see the module docstring):

      * the table's absence -- the app boot never runs Alembic, so once any
        `reference_booted_client` test has run `ensure_reference_table`, the table
        persists in the shared DB for the session (disabled->absent is T-007's
        fresh-DB proof); and
      * a clean 404 for `/reference/state` -- run in isolation the plain boot
        returns 404, but after a prior reference boot the request resolves to 403
        even though `app.router.routes` no longer carries any `/reference` route
        (route resolution leaks across boots on the singleton app). Asserting the
        status code would make this test depend on collection order.

    The order-independent facts below are the honest disabled-side residual: the
    mod is genuinely not enabled/loaded in this boot, and the migration's own gate
    -- the predicate the real upgrade runs -- reads this boot's live config as
    off, so a real upgrade under this enablement would create no table.
    """
    from selfai_ui.main import app

    # The plain boot did not enable or load the reference mod (both order-
    # independent: boot_mods republishes app.state.MODS each boot, and the
    # fixture's monkeypatch of ENABLED_MODS.value is restored on teardown).
    loaded = getattr(app.state, "MODS", None)
    if loaded is not None:
        assert REFERENCE_MOD_ID not in loaded.loaded
    assert REFERENCE_MOD_ID not in config_module.ENABLED_MODS.value

    # The migration's own gate, evaluated against THIS boot's live config, is OFF:
    # had the real upgrade run under this boot's enablement, `upgrade()` would
    # early-return and create no table.
    migration = _load_migration_module()
    assert migration._reference_enabled() is False
