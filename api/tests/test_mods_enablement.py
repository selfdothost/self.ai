"""Enablement is a boot-time decision, and disabled means zero surface.

Both properties are about what does *not* happen, which is the kind of thing
that quietly stops being true. A mod that could appear mid-flight would mean
the surface an operator reviewed is not the surface that is serving; a disabled
mod whose prefix answers differently from an unrouted path would disclose that
it is installed to anyone who guesses.

Cavekit: cavekit-mods-discovery.md R2, R4 — T-041, T-042
"""

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from selfai_ui.mods.loader import boot_mods, run_shutdown_hooks

CORE = "0.5.0"


def _install(root, mod_id, entrypoint, prefix="/x"):
    directory = root / mod_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "mod.yaml").write_text(
        f"id: {mod_id}\nname: {mod_id}\nversion: 0.1.0\n"
        f"entrypoint: {entrypoint}\nmin_core_version: 0.5.0\n"
        f"api:\n  prefix: {prefix}\n",
        encoding="utf-8",
    )


@pytest.fixture
def modmod(request):
    import sys
    import types

    name = f"_enablemod_{request.node.name[:36]}"
    module = types.ModuleType(name)
    sys.modules[name] = module
    yield name, module
    sys.modules.pop(name, None)


def _routed_mod(marker, started=None):
    class Mod:
        def register_routers(self, router: APIRouter):
            @router.get("/ping")
            def _handler():
                return {"from": marker}

        def register_lifecycle(self):
            return {"startup": lambda: started.append(marker)} if started is not None else {}

    return Mod()


def _app_with_core():
    app = FastAPI()

    @app.get("/core")
    def _core():
        return {"from": "core"}

    return app


# --- T-041: enablement takes effect at boot, not while running -------------
@pytest.mark.tier0
def test_adding_to_the_enabled_list_while_running_changes_nothing(tmp_path, modmod):
    name, module = modmod
    module.Later = _routed_mod("later")
    _install(tmp_path, "later", f"{name}:Later", "/later")

    app = _app_with_core()
    enabled: list[str] = []
    boot_mods(app, [tmp_path], enabled, core_version=CORE)

    client = TestClient(app)
    assert client.get("/later/ping").status_code == 404

    # An operator edits the list on a running instance.
    enabled.append("later")

    assert client.get("/later/ping").status_code == 404, "a mod must not appear mid-flight"
    assert client.get("/core").json() == {"from": "core"}


@pytest.mark.tier0
def test_removing_from_the_enabled_list_does_not_unmount_a_running_mod(tmp_path, modmod):
    name, module = modmod
    module.Mod = _routed_mod("alpha")
    _install(tmp_path, "alpha", f"{name}:Mod", "/alpha")

    app = _app_with_core()
    enabled = ["alpha"]
    boot_mods(app, [tmp_path], enabled, core_version=CORE)

    client = TestClient(app)
    assert client.get("/alpha/ping").json() == {"from": "alpha"}

    enabled.remove("alpha")

    assert client.get("/alpha/ping").json() == {"from": "alpha"}, (
        "already-mounted routes must keep serving until the next boot"
    )


@pytest.mark.tier0
def test_after_a_restart_the_loaded_set_matches_the_list_as_it_stood(tmp_path, modmod):
    name, module = modmod
    module.A = _routed_mod("a")
    module.B = _routed_mod("b")
    _install(tmp_path, "a", f"{name}:A", "/a")
    _install(tmp_path, "b", f"{name}:B", "/b")

    first = _app_with_core()
    result = boot_mods(first, [tmp_path], ["a"], core_version=CORE)
    assert result.loaded_ids == ["a"]
    assert TestClient(first).get("/b/ping").status_code == 404

    run_shutdown_hooks(result)

    # "Restart": a fresh application, reading the list as it now stands.
    second = _app_with_core()
    result = boot_mods(second, [tmp_path], ["b"], core_version=CORE)

    assert result.loaded_ids == ["b"]
    client = TestClient(second)
    assert client.get("/b/ping").json() == {"from": "b"}
    assert client.get("/a/ping").status_code == 404, "the previously enabled mod is gone after restart"


# --- T-042: disabled means zero surface ------------------------------------
@pytest.mark.tier0
def test_a_disabled_mods_prefix_is_indistinguishable_from_an_unrouted_path(tmp_path, modmod):
    """Not merely 404 — the *same* 404, so probing discloses nothing."""
    name, module = modmod
    module.Mod = _routed_mod("secret")
    _install(tmp_path, "secret", f"{name}:Mod", "/secret")

    app = _app_with_core()
    boot_mods(app, [tmp_path], [], core_version=CORE)

    client = TestClient(app)
    disabled = client.get("/secret/ping")
    unrouted = client.get("/definitely-not-a-route/ping")

    assert disabled.status_code == unrouted.status_code == 404
    assert disabled.json() == unrouted.json(), "the response must not reveal that a mod is installed"


@pytest.mark.tier0
def test_the_indistinguishability_holds_for_the_bare_prefix_too(tmp_path, modmod):
    name, module = modmod
    module.Mod = _routed_mod("secret")
    _install(tmp_path, "secret", f"{name}:Mod", "/secret")

    app = _app_with_core()
    boot_mods(app, [tmp_path], [], core_version=CORE)

    client = TestClient(app)
    assert client.get("/secret").json() == client.get("/nothing-here").json()


@pytest.mark.tier0
def test_a_disabled_mods_startup_hook_is_never_invoked(tmp_path, modmod):
    """Zero surface includes zero side effects. A disabled mod does not run."""
    name, module = modmod
    started: list[str] = []
    module.Mod = _routed_mod("secret", started=started)
    _install(tmp_path, "secret", f"{name}:Mod", "/secret")

    app = _app_with_core()
    boot_mods(app, [tmp_path], [], core_version=CORE)

    assert started == [], "a disabled mod's code must not execute at all"


@pytest.mark.tier0
def test_disablement_survives_a_restart(tmp_path, modmod):
    name, module = modmod
    started: list[str] = []
    module.Mod = _routed_mod("secret", started=started)
    _install(tmp_path, "secret", f"{name}:Mod", "/secret")

    first = _app_with_core()
    result = boot_mods(first, [tmp_path], ["secret"], core_version=CORE)
    assert result.loaded_ids == ["secret"] and started == ["secret"]
    run_shutdown_hooks(result)

    started.clear()
    second = _app_with_core()
    boot_mods(second, [tmp_path], [], core_version=CORE)

    client = TestClient(second)
    assert client.get("/secret/ping").json() == client.get("/unrouted/ping").json()
    assert started == []


@pytest.mark.tier0
def test_a_disabled_mod_does_not_suppress_an_enabled_one(tmp_path, modmod):
    name, module = modmod
    module.On = _routed_mod("alpha")
    module.Off = _routed_mod("beta")
    _install(tmp_path, "alpha", f"{name}:On", "/alpha")
    _install(tmp_path, "beta", f"{name}:Off", "/beta")

    app = _app_with_core()
    result = boot_mods(app, [tmp_path], ["alpha"], core_version=CORE)

    assert result.loaded_ids == ["alpha"]
    assert result.errors == [], "a merely-disabled mod is not an error"

    client = TestClient(app)
    assert client.get("/alpha/ping").json() == {"from": "alpha"}
    assert client.get("/beta/ping").status_code == 404
