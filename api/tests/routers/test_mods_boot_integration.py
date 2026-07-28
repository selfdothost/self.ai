"""End-to-end mods boot against the REAL application.

This is the test whose absence let F-002 through: every other mods test builds a
throwaway FastAPI() and calls boot_mods directly, so the fact that
selfai_ui.main.app never invoked boot_mods was invisible. This one installs a
real mod on disk, enables it, and drives the app through its actual lifespan
(the `client` fixture uses `with TestClient(app)`, which runs startup/shutdown),
then asserts the mod actually loaded, served, seeded, and registered.

If this test passes, the wiring exists. If the lifespan call is ever removed
again, this fails.

Cavekit: cavekit-mods-discovery.md R1/R4 (boot), R2 (scope seeding);
cavekit-mods-registry.md R1 (endpoint reflects loaded set).
"""

import sys
import textwrap
import types

import pytest
from fastapi.testclient import TestClient

MOD_ID = "boottest"
MOD_ENTRYPOINT = f"_boottest_mod:{'Mod'}"


@pytest.fixture
def installed_mod(tmp_path, monkeypatch):
    """Install a real mod on disk and point the mods config at it.

    The mod mounts a route, declares a scope, and registers a tool — enough to
    prove every boot step ran, not just discovery.
    """
    # A real importable entrypoint module for the mod.
    mod_module = types.ModuleType("_boottest_mod")
    exec(
        textwrap.dedent(
            """
            from fastapi import APIRouter, Request

            class Mod:
                def register_routers(self, router: APIRouter):
                    @router.get("/ping")
                    def ping():
                        return {"mod": "boottest"}

                    # Reads its own declared config the way the contract says a
                    # mod does: the derived MOD_<ID>_<KEY> attribute off
                    # app.state.config, never the environment directly.
                    @router.get("/conf")
                    def conf(request: Request):
                        c = request.app.state.config
                        return {
                            "endpoint": getattr(c, "MOD_BOOTTEST_ENDPOINT", None),
                            "api_key": getattr(c, "MOD_BOOTTEST_API_KEY", None),
                        }

                def register_tools(self):
                    return [{
                        "name": "boottest_tool",
                        "description": "x",
                        "handler": lambda **k: {"ok": True},
                        "scope": "mods.boottest.use",
                    }]
            """
        ),
        mod_module.__dict__,
    )
    sys.modules["_boottest_mod"] = mod_module

    install_dir = tmp_path / "mods"
    (install_dir / MOD_ID).mkdir(parents=True)
    (install_dir / MOD_ID / "mod.yaml").write_text(
        f"id: {MOD_ID}\n"
        f"name: Boot Test\n"
        f"version: 0.1.0\n"
        f"entrypoint: {MOD_ENTRYPOINT}\n"
        f"min_core_version: 0.5.0\n"
        f"api:\n  prefix: /boottest\n"
        f"scopes:\n  - id: mods.boottest.use\n    desc: Use the boot-test mod\n"
        # Both config shapes: an ordinary key with an inline default, and a
        # secret key that must resolve only from the environment.
        f"config:\n"
        f"  - key: endpoint\n    desc: Ordinary key\n    default: https://default.internal\n"
        f"  - key: api_key\n    desc: Secret key\n    secret: true\n",
        encoding="utf-8",
    )

    # The secret's only permitted source. Set before the lifespan runs, because
    # boot is when the PersistentConfig is constructed and reads it.
    monkeypatch.setenv("MOD_BOOTTEST_API_KEY", "from-the-vault")

    # Point core's config at this install dir and enable the mod. Patch the
    # module-level symbols main._boot_mods reads.
    from selfai_ui import config as config_module

    monkeypatch.setattr(config_module, "MODS_INSTALL_DIRS", [install_dir])
    monkeypatch.setattr(config_module.ENABLED_MODS, "value", [MOD_ID], raising=False)

    # The mod declares min_core_version 0.5.0, which the real VERSION now
    # satisfies (api/package.json = 0.5.0, F-008 fixed). No VERSION patch needed
    # — if this ever regresses to the 0.0.0 fallback, this test's "the mod
    # loaded" assertion fails, which is the point.

    yield
    sys.modules.pop("_boottest_mod", None)


@pytest.fixture
def booted_client(test_app, installed_mod):
    """A client whose lifespan runs AFTER the mod is installed and enabled.

    `installed_mod` is listed before the TestClient is entered, so its config
    patches are in place when boot_mods runs during startup. The shared `client`
    fixture cannot guarantee that ordering, which is exactly why this test needs
    its own.

    The route list is snapshotted and restored afterwards. Loading a mod mounts
    its router onto the application and there is deliberately no unmount --
    enablement is a boot-time decision, and a mod that could vanish mid-flight
    would mean the surface an operator reviewed is not the surface serving. That
    is correct for production and leaky in a test process, where the application
    object is shared: without this, `/boottest/*` stayed mounted for every test
    that ran afterwards. It went unnoticed while the route auth audit could not
    see `include_router`-mounted routes at all; once it could (self.ai#51), this
    fixture's leftovers showed up there as unauthenticated routes."""
    before = list(test_app.routes)
    try:
        with TestClient(test_app) as c:
            yield c
    finally:
        test_app.routes[:] = before


@pytest.mark.tier1
def test_boot_actually_loads_an_enabled_mod(booted_client):
    """The whole point: the real app, at boot, loaded the mod we installed."""
    loaded = booted_client.app.state.MODS
    assert loaded is not None, "app.state.MODS must be published by boot — F-002"
    assert MOD_ID in loaded.loaded, f"the enabled mod did not load; errors: {loaded.errors}"


@pytest.mark.tier1
def test_a_loaded_mods_route_serves(booted_client):
    resp = booted_client.get("/boottest/ping")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"mod": "boottest"}


@pytest.mark.tier1
def test_a_loaded_mods_scope_is_seeded_deny_by_default(booted_client, test_app):
    """Permissions R2: the scope is present in the defaults and starts False."""
    defaults = test_app.state.config.USER_PERMISSIONS
    assert defaults.get("mods", {}).get("boottest", {}).get("use") is False, (
        "the mod's scope must be seeded deny-by-default at boot"
    )


@pytest.mark.tier1
def test_a_loaded_mods_declared_config_is_attached_at_boot(booted_client, test_app):
    """Loader R8, through the REAL boot: a declared `config:` key becomes a
    PersistentConfig on AppConfig under the derived MOD_<ID>_<KEY> attribute,
    persisted at `mods.<id>.config.<key>`.

    This is the assertion whose absence let the whole config surface ship inert:
    `build_mod_config`/`attach_mod_config` were unit-tested in
    `test_mods_scopes_and_config.py`, but nothing called them at boot and no test
    looked at the real app, so the published contract's §7 promise was false in
    the running instance. Same shape as F-002 — unit proven, assembly unwired.
    Deleting the `attach_mod_config` call in `boot_mods` must break this.
    """
    app_config = test_app.state.config

    assert app_config.MOD_BOOTTEST_ENDPOINT == "https://default.internal", (
        "an ordinary key must resolve to its manifest default when no env var is set"
    )
    assert app_config.MOD_BOOTTEST_API_KEY == "from-the-vault", (
        "a secret key must resolve from MOD_<ID>_<KEY> in the environment"
    )

    # The persistence path is namespaced per mod, so two mods declaring the same
    # key name cannot read or overwrite each other.
    entry = app_config._state["MOD_BOOTTEST_ENDPOINT"]
    assert entry.config_path == "mods.boottest.config.endpoint"


@pytest.mark.tier1
def test_a_mod_reads_its_own_config_through_its_route(booted_client):
    """The value is reachable by the mod itself, not merely present on AppConfig.

    A mod reads `app.state.config.MOD_<ID>_<KEY>`; it does not read the
    environment. Asserting through the mod's own mounted route is what makes this
    a proof of the read path rather than of the attach path twice.
    """
    resp = booted_client.get("/boottest/conf")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "endpoint": "https://default.internal",
        "api_key": "from-the-vault",
    }


@pytest.mark.tier1
def test_the_registry_endpoint_reflects_the_loaded_mod_for_an_admin(booted_client, test_admin):
    """The registry producer actually ran — the endpoint is not permanently []."""
    booted_client.headers["Authorization"] = f"Bearer {test_admin['token']}"
    resp = booted_client.get("/api/v1/mods/enabled")
    assert resp.status_code == 200, resp.text
    assert MOD_ID in {e["id"] for e in resp.json()}


@pytest.mark.tier1
def test_boot_retains_the_loaded_mods_on_disk_directory(booted_client):
    """frontend-api R2 crit 1, through the REAL boot: the loaded record carries the
    absolute directory the mod loaded from -- the surface R3/R4 will serve assets
    off. Asserted through app.state.MODS, the record boot actually published."""
    loaded = booted_client.app.state.MODS.loaded[MOD_ID]
    directory = loaded.directory
    assert directory is not None and directory.is_absolute()
    assert directory.name == MOD_ID
    assert (directory / "mod.yaml").is_file()


@pytest.mark.tier1
def test_no_mods_enabled_is_a_clean_empty_boot(client):
    """The default path: nothing enabled, app boots, registry is [] not error."""
    loaded = client.app.state.MODS
    assert loaded is not None
    assert loaded.loaded == {}


@pytest.mark.tier1
def test_a_broken_mod_does_not_stop_the_app_booting(tmp_path, monkeypatch, db_session):
    """Failure isolation at the boot level: an unloadable mod is logged and the
    app still comes up serving core routes."""
    install_dir = tmp_path / "mods"
    (install_dir / "brokenmod").mkdir(parents=True)
    (install_dir / "brokenmod" / "mod.yaml").write_text(
        "id: brokenmod\nname: Broken\nversion: 0.1.0\n"
        "entrypoint: _nonexistent_module_xyz:Mod\nmin_core_version: 0.5.0\n",
        encoding="utf-8",
    )

    from selfai_ui import config as config_module

    monkeypatch.setattr(config_module, "MODS_INSTALL_DIRS", [install_dir])
    monkeypatch.setattr(config_module.ENABLED_MODS, "value", ["brokenmod"], raising=False)

    from selfai_ui.main import app

    with TestClient(app) as c:
        # App came up; a core route serves.
        assert c.get("/health").status_code == 200
        # The broken mod is absent and recorded as an error.
        result = app.state.MODS
        assert "brokenmod" not in result.loaded
        assert any("brokenmod" in e for e in result.errors)
