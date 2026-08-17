"""Shared real-boot fixtures for the *reference* mod (Tier 2 — T-009).

This module is the single vehicle the Verification Convention
(`cavekit-mods-overview.md`) requires for Tier 2: a criterion about "at boot" is
satisfied only by starting the real `selfai_ui.main.app` the deployed process
serves, and a caller-facing fact is satisfied only *through its surface*. Every
Tier-2 task (T-010…T-015) and the R8 boot test (T-016) hang off the one fixture
here rather than re-deriving the boot setup.

It mirrors `tests/routers/test_mods_boot_integration.py`'s `installed_mod` /
`booted_client` pair, with two differences:

  1. It installs the **real** `reference` mod at its **real** repo location
     (`api/mods/`), not a synthetic mod copied into `tmp_path`. R7 already proved
     that is where the mod lives; Tier 2 must prove the real installed artifact,
     not a copy.
  2. It cleans up the process-global state the reference mod mutates at boot (the
     shared `sio` namespace and the router mounted on the singleton app), so the
     fixture can boot cleanly once per test across a whole session. The synthetic
     `boottest` mod registers no ws namespace, so its fixture never needed this.

The fixtures here are registered as a pytest plugin in `tests/conftest.py`
(`pytest_plugins = ("tests.mods_reference_boot",)`), so a downstream Tier-2 test
in ANY directory under `tests/` just requests `reference_booted_client` by name —
no import of the fixture (importing it would collide with the parameter name and
trip ruff F811). Only the module constants and the grant helper are imported::

    from tests.mods_reference_boot import REFERENCE_MOD_ID, grant_reference_scope_for_everyone

    def test_something(reference_booted_client):        # provided by the plugin
        boot = reference_booted_client
        boot.as_admin().get("/reference/state")          # authed as the admin
        boot.app.state.MODS.loaded                        # {"reference": LoadedMod}

See `context/impl/impl-mods-reference-implementation.md` (Tier 2 / T-009) for the
full consumer guide.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect as sa_inspect

#: The REAL on-disk location of the mods install root. `tests/` sits directly
#: under `api/`, so `parent.parent` is the `api/` root and `api/mods/` is the
#: directory that holds `reference/`. Discovery is pointed here (not a tmp_path
#: copy) so Tier 2 proves the real installed artifact.
API_ROOT = Path(__file__).resolve().parent.parent
REFERENCE_MODS_DIR = API_ROOT / "mods"

#: The mod under proof.
REFERENCE_MOD_ID = "reference"

#: The one namespace the reference mod registers on core's shared Socket.IO
#: server. It must be torn back off `sio.handlers` after each boot or the second
#: boot in a session raises `ModLoadError` ("namespace already registered").
REFERENCE_NAMESPACE = "/reference"

#: The one route prefix the reference mod mounts on the singleton app. Its routes
#: must be popped after each boot so they do not accumulate across a session.
REFERENCE_PREFIX = "/reference"


#: The one table the reference mod owns. Derived, not hardcoded, so the fixture
#: agrees with the migration's authority on the name.
def _reference_table_name() -> str:
    from selfai_ui.mods.naming import table_prefix_for

    return f"{table_prefix_for(REFERENCE_MOD_ID)}handles"


# ---------------------------------------------------------------------------
# The migration-run guarantee (item 3 of the T-009 brief)
# ---------------------------------------------------------------------------
#
# CONFIRMED, not assumed: `tests/conftest.py` runs `alembic upgrade head` ONCE at
# session import (conftest.py:67), and it does so BEFORE `reference` is enabled.
# The reference mod's migration (`b7e1c0ffee42`) gates its `upgrade()` on the mod
# being enabled, so at that session-start upgrade its `upgrade()` early-returns
# and `mod_reference_handles` is NEVER created. `TestClient(app)`'s lifespan runs
# `boot_mods` but never Alembic, so entering the client does not create it either.
#
# So the fixture must make the table exist itself. It does so by running the REAL
# migration under enablement — stamp the version pointer back one revision, then
# `upgrade head` with `reference` enabled so `b7e1c0ffee42.upgrade()` actually
# runs its `op.create_table`. This is the genuine revision creating the genuine
# table, not a hand-rolled CREATE TABLE that could drift from it. It is guarded by
# an existence check so it runs at most once per session, and it leaves the
# version pointer back at head. (T-014 is the task that PROVES this migration's
# enablement-gating across DB states; here we only need the table present so the
# R6 survival check in T-015 and the R5 presence check in T-016 have something to
# read.)

# The revision immediately below the reference migration — its `down_revision`
# (`b7e1c0ffee42_add_mod_reference_handles_table.py:58`). Stamping here lets the
# reference revision re-run under enablement.
_REVISION_BELOW_REFERENCE = "d5e6f7a8b9c0"

# The reference mod's own migration — the exact revision this stamp-down + reapply
# exists to replay, and the ONLY one that should run here. We upgrade to THIS
# revision, never to "head": "head" is a moving target, and any migration stacked
# ABOVE reference (e.g. e7d2a9c1f3b0, the vram_consumer table) was already applied
# by conftest's session-start `upgrade head`. Those are plain `op.create_table`
# calls, not re-run-safe no-ops, so replaying them raises "table ... already
# exists". Targeting the reference revision runs exactly the migration this
# fixture is here to run and nothing above it.
_REFERENCE_REVISION = "b7e1c0ffee42"


def _alembic_config():
    """An Alembic config pointed at the test DB, built the way conftest builds
    it (conftest.py:56-66) so it targets the same file-backed SQLite DB."""
    from alembic.config import Config as AlembicConfig

    ini = Path("/app/backend/selfai_ui/alembic.ini")
    if not ini.exists():
        ini = API_ROOT / "selfai_ui" / "alembic.ini"
    migrations = Path("/app/backend/selfai_ui/migrations")
    if not migrations.exists():
        migrations = API_ROOT / "selfai_ui" / "migrations"

    cfg = AlembicConfig(str(ini))
    cfg.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])
    cfg.set_main_option("script_location", str(migrations))
    return cfg


def ensure_reference_table(engine) -> None:
    """Make `mod_reference_handles` exist by running the real migration under
    enablement. Idempotent: a no-op once the table is present.

    Patches `config.ENABLED_MODS.value` (not the env var) for the duration of the
    upgrade, because the migration reads enablement from the already-imported
    `selfai_ui.config` module, not from the environment, once config is loaded
    (`migrations/util.py:enabled_mods` -- shared by every mod-owned revision
    since self.ai#85 P0; it used to be a copy inside each one).
    """
    if _reference_table_name() in sa_inspect(engine).get_table_names():
        return

    from alembic import command as alembic_command

    from selfai_ui import config as config_module

    cfg = _alembic_config()
    # Pretend we are one revision below the reference migration so it re-runs.
    alembic_command.stamp(cfg, _REVISION_BELOW_REFERENCE)

    original = list(config_module.ENABLED_MODS.value)
    config_module.ENABLED_MODS.value = [REFERENCE_MOD_ID]
    try:
        # Upgrade to the reference revision specifically, NOT "head" — see the
        # _REFERENCE_REVISION note. Replaying migrations above reference would
        # re-create tables conftest already made and raise "table already exists".
        alembic_command.upgrade(cfg, _REFERENCE_REVISION)
    finally:
        config_module.ENABLED_MODS.value = original
    # Restore the version pointer to the true head. The migrations above reference
    # already created their tables at conftest's session-start `upgrade head`, so
    # stamp (record the version) rather than upgrade (re-execute) — the DB reads
    # as head without re-running anything.
    alembic_command.stamp(cfg, "head")


# ---------------------------------------------------------------------------
# The fixture
# ---------------------------------------------------------------------------


@dataclass
class ReferenceBoot:
    """What `reference_booted_client` yields.

    `client`    — a `TestClient` bound to the REAL booted app (its lifespan ran
                  `boot_mods` with the reference mod installed + enabled).
                  Unauthenticated by default; set a Bearer header per request or
                  use `as_admin()` / `auth()`.
    `app`       — the real `selfai_ui.main.app`, post-boot. Read `app.state.MODS`
                  (the `LoadResult`; `.loaded["reference"]`) and
                  `app.state.config.USER_PERMISSIONS` (the seeded scope defaults).
    `admin`     — a freshly minted admin user dict `{id,email,role,token}` (from
                  the shared `test_admin` fixture), for the admin round trips
                  T-012/T-016 need (permissions GET→POST→GET, registry read).
    """

    client: TestClient
    app: FastAPI
    admin: dict

    def auth(self, token: str) -> TestClient:
        """Set the client's Authorization header to `token` and return it.

        Note the header persists on the shared client until changed — pass an
        explicit `headers=` per request instead if a test alternates between a
        scoped and an unscoped caller."""
        self.client.headers["Authorization"] = f"Bearer {token}"
        return self.client

    def as_admin(self) -> TestClient:
        """The client authenticated as the fixture's admin user."""
        return self.auth(self.admin["token"])


@pytest.fixture
def reference_booted_client(test_app, test_admin, test_engine, monkeypatch):
    """Boot the REAL app with the REAL `reference` mod installed and enabled.

    Ordering mirrors `booted_client`: the config patches (install dir + enabled
    list) and the table guarantee are all in place BEFORE `TestClient(test_app)`
    is entered, so `boot_mods` — which runs during the lifespan startup — sees
    them. `test_app` brings the DB override and startup-task isolation; the real
    lifespan otherwise runs unmodified.

    Teardown removes the process-global state the reference mod mutates at boot —
    the `/reference` `sio` namespace and the `/reference` router mounted on the
    singleton app — so the next test in the session boots cleanly.

    Yields a `ReferenceBoot`.
    """
    from selfai_ui import config as config_module
    from selfai_ui.socket.main import sio

    # 1. Point discovery at the real mods root and enable ONLY the reference mod.
    #    `_boot_mods` reads these two module-level symbols from `selfai_ui.config`
    #    at boot, so patching them there is what the lifespan sees.
    monkeypatch.setattr(config_module, "MODS_INSTALL_DIRS", [REFERENCE_MODS_DIR])
    monkeypatch.setattr(config_module.ENABLED_MODS, "value", [REFERENCE_MOD_ID], raising=False)

    # 2. Guarantee the mod's table exists in the boot-path DB (see module notes).
    ensure_reference_table(test_engine)

    # 3. Drive the real lifespan. Startup runs boot_mods -> the reference mod
    #    loads, mounts its route, registers its namespace, seeds its scope, and
    #    runs its startup callback exactly once.
    with TestClient(test_app) as client:
        try:
            yield ReferenceBoot(client=client, app=test_app, admin=test_admin)
        finally:
            # Roll the shared Socket.IO namespace back off core's server so a
            # second boot in this session does not hit the loader's
            # "namespace already registered" guard (loader.py:253).
            sio.handlers.pop(REFERENCE_NAMESPACE, None)
            # Drop the routes this boot mounted so they do not accumulate on the
            # singleton app across the session.
            test_app.router.routes = [
                r for r in test_app.router.routes if not getattr(r, "path", "").startswith(REFERENCE_PREFIX)
            ]


# ---------------------------------------------------------------------------
# Scope grant/revoke — provided as helpers, NOT baked into the fixture
# ---------------------------------------------------------------------------
#
# DESIGN DECISION (the load-bearing one for the six tasks after T-009):
# the base fixture boots the mod with its scope seeded DENY-BY-DEFAULT and grants
# it to nobody. Granting is left to the consumer, because R3 (T-012) is precisely
# the task that must PROVE the grant path through the admin round trip, and
# because "unscoped vs scoped" is a per-consumer need (T-012 wants BOTH a granted
# and an ungranted caller in one test; T-013/T-015 want an authenticated caller
# who never needs the scope at all). Two grant paths exist; pick per need:
#
#   * INSTANCE DEFAULT (grants EVERYONE) — the literal `cavekit-mods-permissions`
#     R3 "GET -> POST -> GET" round trip against `/api/v1/users/default/permissions`.
#     Use `grant_reference_scope_for_everyone(admin_client)` below. Suitable when
#     a test only needs "before any grant" then "after the grant" in sequence.
#
#   * GROUP GRANT (grants ONE user) — create a group carrying
#     `{"mods": {"reference": {"use": True}}}` with the target user in it, the way
#     `tests/routers/test_mods_permissions_closeout.py::_group_granting` does.
#     Use this when a test needs a scoped AND an unscoped user simultaneously.
#     Left to the consumer since it needs a `db_session` and a specific user id.


def grant_reference_scope_for_everyone(admin_client: TestClient) -> dict:
    """Grant `mods.reference.use` to the whole instance via the real admin
    permissions round trip (GET -> mutate -> POST -> GET), the shape
    `cavekit-mods-permissions.md` R3 pins. Returns the permissions object the
    server echoes back after the write.

    `admin_client` must already carry an admin Bearer token (use
    `ReferenceBoot.as_admin()`). This flips the seeded deny-by-default leaf to
    True through the endpoint — it does NOT mutate `USER_PERMISSIONS` directly,
    which is the forbidden shortcut.
    """
    current = admin_client.get("/api/v1/users/default/permissions")
    assert current.status_code == 200, current.text
    perms = current.json()
    perms.setdefault("mods", {}).setdefault(REFERENCE_MOD_ID, {})["use"] = True

    saved = admin_client.post("/api/v1/users/default/permissions", json=perms)
    assert saved.status_code == 200, saved.text

    confirmed = admin_client.get("/api/v1/users/default/permissions")
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()
