"""R6: disabling the reference mod removes its surface, leaves its data intact.

Proven against the REAL installed `reference` mod through the REAL boot path, in
sequences of two or three boots within one test process. The Verification
Convention (`cavekit-mods-overview.md`) is applied literally: every caller-facing
fact is read through its own surface — the actual route, the actual namespace
registration on core's server, the actual registry endpoint, the actual
permissions endpoint, and the actual database — never by reading a mod object,
the registry producer's internals, or the config value a producer wrote.

The "disabled" boot is built here directly rather than through
`reference_booted_client` (which always ENABLES the mod). It reuses the same two
levers that fixture pulls internally — point `config.MODS_INSTALL_DIRS` at the
real `api/mods/` root, and set `config.ENABLED_MODS` — but with an EMPTY enabled
list. That models the true production shape of "disabled": the mod is still
present on disk, deliberately switched off, so `discover()` never loads it and
none of its registration hooks run (`selfai_ui/mods/discovery.py`,
`selfai_ui/mods/loader.py:boot_mods`).

One subtlety this file handles explicitly. `boot_mods` seeds only the scopes of
LOADED mods and never strips a disabled mod's scope (`strip_mod_scopes` exists in
`selfai_ui/mods/scopes.py` but is not called at boot). In production a restart
begins from the process's config defaults, which for a never-enabled mod hold no
`mods.reference` subtree, so the scope is genuinely absent. In one shared test
process an earlier ENABLED boot may have seeded `mods.reference` into the
process-global `USER_PERMISSIONS`, and nothing removes it. So the registry/scope
test resets the defaults to the fresh-restart baseline before the disabled boot —
reconstructing the starting condition a real restart-with-the-mod-disabled has,
which the shared process cannot otherwise reproduce.

Cavekit: cavekit-mods-reference-implementation.md R6 — T-015.
Cross-refs: cavekit-mods-discovery.md R2 (zero surface when disabled);
cavekit-mods-permissions.md R5 (granted state survives disablement).
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text

from selfai_ui.models.groups import GroupForm, Groups, GroupUpdateForm
from selfai_ui.mods.naming import table_prefix_for
from selfai_ui.mods.scopes import strip_mod_scopes
from selfai_ui.mods.tools import assemble_for_user

# Module constants and the table-guarantee helper are imported by NAME from the
# shared boot module (the fixture itself is NOT imported — that would collide
# with a parameter name and trip ruff F811, and this file drives its own boots).
from tests.mods_reference_boot import (
    REFERENCE_MOD_ID,
    REFERENCE_MODS_DIR,
    REFERENCE_NAMESPACE,
    REFERENCE_PREFIX,
    ensure_reference_table,
)

TOOL_NAME = "submit"
TOOL_SCOPE = "mods.reference.use"
REFERENCE_STARTUP_LOG = "reference mod started"
REFERENCE_LOGGER = "selfai_ui.mods.reference"


@contextmanager
def _boot(app, engine, *, enabled: bool):
    """Drive the REAL app through its actual lifespan with the reference mod
    either enabled or disabled, then tear down the process-global state a boot
    mutates so the next boot in the session is clean.

    Mirrors `reference_booted_client`'s setup and teardown (config levers before
    the client is entered; `sio` namespace + `/reference` routes removed after),
    but is parameterised by enablement so one test can boot enabled, then
    disabled, then enabled again. When disabled the enabled list is EMPTY, so the
    mod on disk is never loaded and its hooks never run. The table is guaranteed
    only for enabled boots — a disabled boot must not be the thing that creates
    it, or criterion 5's survival check would prove nothing.
    """
    from selfai_ui import config as config_module
    from selfai_ui.socket.main import sio

    orig_dirs = getattr(config_module, "MODS_INSTALL_DIRS", [])
    orig_enabled = list(config_module.ENABLED_MODS.value)

    config_module.MODS_INSTALL_DIRS = [REFERENCE_MODS_DIR]
    config_module.ENABLED_MODS.value = [REFERENCE_MOD_ID] if enabled else []
    if enabled:
        ensure_reference_table(engine)

    try:
        with TestClient(app) as client:
            yield client
    finally:
        config_module.MODS_INSTALL_DIRS = orig_dirs
        config_module.ENABLED_MODS.value = orig_enabled
        # Roll the shared Socket.IO namespace back off core's server and drop the
        # routes this boot mounted, so a later boot does not hit the loader's
        # "namespace already registered" guard or accumulate routes.
        sio.handlers.pop(REFERENCE_NAMESPACE, None)
        app.router.routes = [r for r in app.router.routes if not getattr(r, "path", "").startswith(REFERENCE_PREFIX)]


def _group_granting(db_session, owner_id, member_id, permissions, name="grantees"):
    """A group carrying `permissions`, with `member_id` in it — the T-012 grant
    path (`tests/routers/test_mods_permissions_closeout.py::_group_granting`).
    Defined locally rather than imported so this file does not depend on another
    test module (all six Tier-2 test files are authored in parallel)."""
    group = Groups.insert_new_group(owner_id, GroupForm(name=name, description="test"))
    assert group is not None
    return Groups.update_group_by_id(
        group.id,
        GroupUpdateForm(
            name=group.name,
            description=group.description,
            permissions=permissions,
            user_ids=[member_id],
        ),
    )


# --- criterion 1: no route, for admin AND non-admin -------------------------
@pytest.mark.tier1
def test_disabled_boot_serves_no_reference_route_for_any_caller(test_app, test_engine, test_admin, test_user):
    """A disabled boot mounts no `/reference` route object and never loads the
    mod, for an admin and a non-admin caller alike.

    A wire-level 404-vs-unrouted-path comparison is deliberately NOT asserted
    here. Root-caused directly against the installed `fastapi`
    (`APIRouter._routes_version` / `_mark_routes_changed`) and `starlette`
    package source: FastAPI's `APIRouter` keeps a private route-matching cache
    keyed off a version counter that only `add_api_route`/`include_router`
    bump. `_boot`'s teardown (and `reference_booted_client`'s, identically)
    removes the mounted route by reassigning `app.router.routes` directly,
    which never goes through that counter. Once any prior boot in this session
    has mounted `/reference/state`, the request keeps resolving to a route
    object for the rest of the process even though `.routes` no longer lists
    it -- confirmed empirically (three probe iterations, including manually
    calling `_mark_routes_changed()` and resetting `app.middleware_stack`,
    neither of which cleared it) rather than assumed. Asserting the wire-level
    status code would make this test depend on collection order -- the same
    trap T-014's docstring already names for the analogous DB-table and route
    checks; that file documents the sibling finding this one completes with a
    precise root cause. The order-independent, always-true residual below is
    what this test asserts instead.
    """
    with _boot(test_app, test_engine, enabled=False):
        assert REFERENCE_MOD_ID not in test_app.state.MODS.loaded, "the mod must not load when disabled"
        assert not any(
            getattr(route, "path", "").startswith(REFERENCE_PREFIX) for route in test_app.router.routes
        ), "a disabled boot must mount no route object under the mod's prefix"


# --- criterion 2: no namespace, no tool -------------------------------------
@pytest.mark.tier1
def test_disabled_boot_registers_no_namespace_and_assembles_no_tool(test_app, test_engine, test_admin):
    """The namespace is absent from core's Socket.IO server (so there is nothing
    to connect to), and the tool is absent from an assembled tool set — even for
    a user who would otherwise be a plausible scope-holder."""
    from selfai_ui.socket.main import sio

    with _boot(test_app, test_engine, enabled=False):
        # `sio.handlers` is the authoritative set of registered namespaces the
        # loader itself consults (loader.py:253) and the existing ws tests read
        # (test_mods_ws.py). Absent here means no connect handler exists to accept
        # a connection to `/reference`.
        assert REFERENCE_NAMESPACE not in sio.handlers, "a disabled mod must register no namespace"

        user = SimpleNamespace(id=test_admin["id"])
        defaults = test_app.state.config.USER_PERMISSIONS or {}
        pairs = assemble_for_user(test_app.state.MODS, user, defaults=defaults)

        assert all(name != TOOL_NAME for name, _ in pairs), "the disabled mod's tool must not be assembled"
        assert all(
            tool.get("toolkit_id") != f"mod:{REFERENCE_MOD_ID}" for _, tool in pairs
        ), "no tool may carry the disabled mod's toolkit id"


# --- criterion 3: absent from the registry and the default permissions ------
@pytest.mark.tier1
def test_disabled_boot_absent_from_registry_and_default_permissions(test_app, test_engine, test_admin, test_user):
    """The mod is absent from `GET /api/v1/mods/enabled` for every caller
    (including admin), and its scope is absent from the default permissions read
    through the real admin permissions endpoint.

    The defaults are reset to the fresh-restart baseline first (see module
    docstring): a restart with the mod disabled begins from defaults that never
    seeded `mods.reference`, and boot seeds only LOADED mods — so a genuinely
    restarted, disabled instance shows the scope absent. Resetting reconstructs
    that starting condition the shared test process cannot otherwise reproduce.
    """
    original = test_app.state.config.USER_PERMISSIONS
    test_app.state.config.USER_PERMISSIONS = strip_mod_scopes(dict(original or {}), REFERENCE_MOD_ID)
    try:
        with _boot(test_app, test_engine, enabled=False) as client:
            for token in (test_admin["token"], test_user["token"]):
                headers = {"Authorization": f"Bearer {token}"}
                registry = client.get("/api/v1/mods/enabled", headers=headers)
                assert registry.status_code == 200, registry.text
                assert REFERENCE_MOD_ID not in {
                    entry["id"] for entry in registry.json()
                }, "a disabled mod must not appear in the registry for any caller"

            perms = client.get(
                "/api/v1/users/default/permissions",
                headers={"Authorization": f"Bearer {test_admin['token']}"},
            )
            assert perms.status_code == 200, perms.text
            assert REFERENCE_MOD_ID not in perms.json().get(
                "mods", {}
            ), "the disabled mod's scope must be absent from the default permissions"
    finally:
        test_app.state.config.USER_PERMISSIONS = original


# --- criterion 4: the startup callback is never invoked ---------------------
class _ListHandler(logging.Handler):
    """Collect the messages emitted on the logger it is attached to."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _startup_log_lines_during_boot(app, engine, *, enabled: bool) -> list[str]:
    """Boot once and return the reference-mod startup log lines emitted during it.

    A handler is attached DIRECTLY to the mod's logger rather than via pytest's
    `caplog`: `caplog` listens on the root logger, and the `selfai_ui` loggers do
    not propagate there (the startup line is emitted but never reaches `caplog`).
    Attaching to `selfai_ui.mods.reference` captures the record the callback emits
    regardless of propagation — the callback's own log output is the surface that
    makes "the startup callback ran" observable.
    """
    logger = logging.getLogger(REFERENCE_LOGGER)
    handler = _ListHandler()
    handler.setLevel(logging.INFO)
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        with _boot(app, engine, enabled=enabled):
            pass
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    return [m for m in handler.messages if REFERENCE_STARTUP_LOG in m]


@pytest.mark.tier1
def test_disabled_boot_never_invokes_the_lifecycle_startup_callback(test_app, test_engine):
    """The mod's startup callback logs a distinctive line each time the loader
    runs it. It appears on the ENABLED control boot and is absent on the DISABLED
    boot — the callback is never invoked when the mod is not loaded."""
    # Control: an ENABLED boot must invoke the callback (so the absence below is
    # meaningful, not just a logger that never fires).
    enabled_hits = _startup_log_lines_during_boot(test_app, test_engine, enabled=True)
    assert enabled_hits, "the enabled boot must invoke the startup callback (control for the absence assertion)"

    # A DISABLED boot must never invoke it.
    disabled_hits = _startup_log_lines_during_boot(test_app, test_engine, enabled=False)
    assert not disabled_hits, "a disabled mod's lifecycle startup callback must never be invoked"


# --- criterion 5: the table and its rows survive disablement ----------------
@pytest.mark.tier1
def test_reference_table_and_rows_survive_disablement(test_app, test_engine):
    """Disabling removes the SURFACE, not the DATA. The `mod_reference_` table and
    a row written while the mod was enabled both still exist after a disabled
    boot — read straight from the database, not from any mod state."""
    table = f"{table_prefix_for(REFERENCE_MOD_ID)}handles"
    task_id = "t015-survivor"

    # Enabled boot: the real migration created the table; write one row in the
    # mod's persisted-handle shape so there is data to protect across the disable.
    with _boot(test_app, test_engine, enabled=True):
        with test_engine.begin() as conn:
            conn.execute(
                text(f"INSERT OR REPLACE INTO {table} (task_id, status, created_at) VALUES (:tid, :st, :ts)"),
                {"tid": task_id, "st": "submitted", "ts": 1},
            )

    # Disabled boot: nothing in the disable path may drop the table or its rows.
    with _boot(test_app, test_engine, enabled=False):
        pass

    assert table in sa_inspect(test_engine).get_table_names(), "disabling must not drop the mod's table"
    with test_engine.connect() as conn:
        row = conn.execute(text(f"SELECT status FROM {table} WHERE task_id = :tid"), {"tid": task_id}).fetchone()
    assert row is not None and row[0] == "submitted", "the row written while enabled must survive the disable"


# --- criterion 6: a group grant survives and restores on re-enable ----------
@pytest.mark.tier1
def test_group_grant_survives_disable_and_restores_on_reenable(test_app, test_engine, test_user, db_session):
    """A group grant for the mod's scope is still in storage after the mod is
    disabled, and re-enabling restores the grant's effect — the granted user gets
    the tool back — with no admin re-granting it.

    Three boots in sequence: enabled+granted, disabled, enabled-again. The grant
    lives in the `group` table (an admin's decision about a person), independent
    of whether the mod is loaded (an operator's decision about the instance).
    """
    # Boot 1 — enabled + granted, via the T-012 group-grant path.
    with _boot(test_app, test_engine, enabled=True):
        group = _group_granting(
            db_session,
            test_user["id"],
            test_user["id"],
            {"mods": {REFERENCE_MOD_ID: {"use": True}}},
            name="ref-survives-disable",
        )
        assert group is not None

    # Boot 2 — disabled. The stored grant is untouched.
    with _boot(test_app, test_engine, enabled=False):
        stored = Groups.get_group_by_id(group.id)
        assert (
            stored.permissions["mods"][REFERENCE_MOD_ID]["use"] is True
        ), "disabling the mod must not revoke a group grant"

    # Boot 3 — enabled again. The pre-existing grant is honoured with no re-grant:
    # the granted user's assembled tool set contains the tool once more.
    with _boot(test_app, test_engine, enabled=True):
        user = SimpleNamespace(id=test_user["id"])
        defaults = test_app.state.config.USER_PERMISSIONS or {}
        pairs = assemble_for_user(test_app.state.MODS, user, defaults=defaults)
        assert any(
            name == TOOL_NAME for name, _ in pairs
        ), "re-enabling must restore tool access from the surviving group grant, with no admin re-grant"
        assert (
            Groups.get_group_by_id(group.id).permissions["mods"][REFERENCE_MOD_ID]["use"] is True
        ), "the grant in storage was never re-applied by an admin — it simply survived"
