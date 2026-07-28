"""The loader: entrypoint resolution, mounting, isolation, lifecycle.

Every isolation claim here is exercised against a real FastAPI app through a
real client rather than asserted structurally. An earlier draft of the
request-isolation mechanism used router middleware, which `APIRouter` does not
support -- it would have been a silent no-op that a structural test would have
happily passed.

Cavekit: cavekit-mods-loader.md R1-R7 — T-025..T-037
"""

import sys
import types

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from selfai_ui.mods.loader import (
    CONTRACT_HOOKS,
    LOG_NAMESPACE,
    ModLoadError,
    load_mods,
    mod_logger,
    prefix_conflict,
    resolve_entrypoint,
    run_shutdown_hooks,
)
from selfai_ui.mods.manifest import ModManifest

BASE = {
    "name": "Example",
    "version": "0.1.0",
    "min_core_version": "0.5.0",
}


def _manifest(mod_id, entrypoint, prefix=None):
    data = {**BASE, "id": mod_id, "entrypoint": entrypoint}
    if prefix:
        data["api"] = {"prefix": prefix}
    return ModManifest(**data)


@pytest.fixture
def modmod(request):
    """Install a throwaway module holding mod objects, and clean it up."""
    name = f"_testmod_{request.node.name[:40]}"
    module = types.ModuleType(name)
    sys.modules[name] = module
    yield name, module
    sys.modules.pop(name, None)


def _mod_with_route(path="/ping", payload=None, raises=None):
    class Mod:
        def register_routers(self, router: APIRouter):
            @router.get(path)
            def _handler():
                if raises is not None:
                    raise raises
                return payload or {"ok": True}

    return Mod()


# --- T-025 / T-026: entrypoint resolution ----------------------------------
@pytest.mark.tier0
def test_entrypoint_resolves_an_object_exposing_the_contract(modmod):
    name, module = modmod
    module.Mod = _mod_with_route()
    assert resolve_entrypoint(_manifest("alpha", f"{name}:Mod")) is module.Mod


@pytest.mark.tier0
def test_a_class_entrypoint_is_instantiated(modmod):
    name, module = modmod

    class Mod:
        def register_tools(self):
            return []

    module.Mod = Mod
    resolved = resolve_entrypoint(_manifest("alpha", f"{name}:Mod"))
    assert isinstance(resolved, Mod)


@pytest.mark.tier0
@pytest.mark.parametrize("entrypoint", ["no_colon_here", "nonexistent_module_xyz:Mod"])
def test_unresolvable_entrypoints_fail_to_load(entrypoint):
    with pytest.raises(ModLoadError):
        resolve_entrypoint(_manifest("alpha", entrypoint))


@pytest.mark.tier0
def test_a_missing_attribute_fails_to_load(modmod):
    name, _ = modmod
    with pytest.raises(ModLoadError) as excinfo:
        resolve_entrypoint(_manifest("alpha", f"{name}:Absent"))
    assert "Absent" in str(excinfo.value)


@pytest.mark.tier0
def test_an_object_exposing_no_contract_hook_is_refused(modmod):
    name, module = modmod
    module.Mod = types.SimpleNamespace(unrelated=lambda: None)
    with pytest.raises(ModLoadError) as excinfo:
        resolve_entrypoint(_manifest("alpha", f"{name}:Mod"))
    assert all(hook in str(excinfo.value) for hook in CONTRACT_HOOKS)


@pytest.mark.tier0
def test_a_subset_of_hooks_is_valid(modmod):
    """A mod contributing only tools is as valid as one contributing routes."""
    name, module = modmod
    module.Mod = types.SimpleNamespace(register_tools=lambda: [])
    assert resolve_entrypoint(_manifest("alpha", f"{name}:Mod")) is module.Mod


# --- sys.path gap: a mod installed on disk (not pre-loaded into sys.modules,
# not already on sys.path/PYTHONPATH) must actually be importable ----------
@pytest.mark.tier0
def test_a_mod_installed_on_disk_is_importable_without_pythonpath(tmp_path):
    """Discovery finds a manifest by reading it off disk directly -- it never
    touches `sys.path`. Resolving the entrypoint is the step that actually
    needs the mod's directory to be importable, and nothing wired that until
    now (self.crew#141): a mod dropped via `MODS_EXTRA_DIRS` had a discoverable
    manifest but genuinely unimportable code unless the deploy environment
    happened to also set `PYTHONPATH`, which self.ai's deploy manifests don't.

    Every `modmod`-backed test elsewhere in this file sidesteps the issue
    entirely by injecting straight into `sys.modules`, which Python's import
    system checks before ever consulting `sys.path` -- so none of them could
    have caught this. This test uses a real file on a real temp directory and
    a module name that has never been imported, so it genuinely exercises
    `sys.path` resolution.
    """
    from selfai_ui.mods.discovery import discover

    mod_dir = tmp_path / "diskmod"
    mod_dir.mkdir()
    (mod_dir / "mod.yaml").write_text(
        "id: diskmod\nname: Diskmod\nversion: 0.1.0\n"
        "entrypoint: diskmod_entry:Mod\nmin_core_version: 0.5.0\n",
        encoding="utf-8",
    )
    (mod_dir / "diskmod_entry.py").write_text(
        "class Mod:\n    def register_tools(self):\n        return []\n",
        encoding="utf-8",
    )

    assert "diskmod_entry" not in sys.modules  # a genuinely fresh import

    result = discover([tmp_path], ["diskmod"], core_version="0.5.0")
    assert result.errors == []
    manifest = result.loaded["diskmod"]

    try:
        resolved = resolve_entrypoint(manifest)
        assert type(resolved).__name__ == "Mod"
    finally:
        sys.modules.pop("diskmod_entry", None)
        sys.path.remove(str(manifest.directory))


@pytest.mark.tier0
def test_ensure_importable_does_not_grow_sys_path_on_repeat_calls(tmp_path):
    """Loading several mods across one boot must not pile up duplicate
    `sys.path` entries for the same directory."""
    from selfai_ui.mods.loader import _ensure_importable

    directory = tmp_path / "repeatmod"
    directory.mkdir()

    _ensure_importable(directory)
    length_after_first = len(sys.path)
    _ensure_importable(directory)
    try:
        assert len(sys.path) == length_after_first
        assert sys.path.count(str(directory)) == 1
    finally:
        sys.path.remove(str(directory))


# --- T-027 / T-028: router mounting ----------------------------------------
@pytest.mark.tier0
def test_a_mods_routes_are_reachable_under_its_prefix(modmod):
    name, module = modmod
    module.Mod = _mod_with_route("/ping", {"from": "alpha"})

    app = FastAPI()
    result = load_mods(app, [_manifest("alpha", f"{name}:Mod", "/alpha")])
    assert result.loaded_ids == ["alpha"], result.errors

    client = TestClient(app)
    assert client.get("/alpha/ping").json() == {"from": "alpha"}
    assert client.get("/ping").status_code == 404, "must not be reachable outside its prefix"


@pytest.mark.tier0
def test_two_mods_serve_without_cross_talk(modmod):
    name, module = modmod
    module.A = _mod_with_route("/ping", {"from": "a"})
    module.B = _mod_with_route("/ping", {"from": "b"})

    app = FastAPI()
    result = load_mods(
        app,
        [_manifest("a", f"{name}:A", "/a"), _manifest("b", f"{name}:B", "/b")],
    )
    assert result.loaded_ids == ["a", "b"], result.errors

    client = TestClient(app)
    assert client.get("/a/ping").json() == {"from": "a"}
    assert client.get("/b/ping").json() == {"from": "b"}


@pytest.mark.tier0
def test_a_mod_with_no_api_block_mounts_nothing(modmod):
    name, module = modmod
    module.Mod = types.SimpleNamespace(register_tools=lambda: [])

    app = FastAPI()
    before = len(app.routes)
    result = load_mods(app, [_manifest("alpha", f"{name}:Mod")])

    assert result.loaded_ids == ["alpha"]
    assert len(app.routes) == before


# --- T-029 / T-030: prefix collisions --------------------------------------
@pytest.mark.tier0
@pytest.mark.parametrize(
    "a,b,conflicts",
    [
        ("/crew", "/crew", True),
        ("/crew", "/crew/api", True),
        ("/crew/api", "/crew", True),
        ("/crew", "/crewing", False),
        ("/crew", "/other", False),
        ("crew", "/crew/", True),
    ],
)
def test_prefix_conflict_detects_shadowing_not_just_equality(a, b, conflicts):
    assert prefix_conflict(a, b) is conflicts


@pytest.mark.tier0
def test_a_mod_colliding_with_core_is_refused_and_core_still_serves(modmod):
    name, module = modmod
    module.Mod = _mod_with_route("/ping", {"from": "mod"})

    app = FastAPI()

    @app.get("/api/core")
    def _core():
        return {"from": "core"}

    result = load_mods(app, [_manifest("alpha", f"{name}:Mod", "/api")])

    assert result.loaded == {}
    assert "conflicts" in result.errors[0]
    assert TestClient(app).get("/api/core").json() == {"from": "core"}


@pytest.mark.tier0
def test_mod_to_mod_collision_refuses_the_second_and_keeps_the_first(modmod):
    name, module = modmod
    module.A = _mod_with_route("/ping", {"from": "a"})
    module.B = _mod_with_route("/ping", {"from": "b"})

    app = FastAPI()
    result = load_mods(
        app,
        [_manifest("a", f"{name}:A", "/shared"), _manifest("b", f"{name}:B", "/shared/sub")],
    )

    assert result.loaded_ids == ["a"]
    assert len(result.errors) == 1 and "b" in result.errors[0]
    assert TestClient(app).get("/shared/ping").json() == {"from": "a"}


# --- T-033 / T-034: registration isolation, no partial surface -------------
@pytest.mark.tier0
def test_a_mod_raising_during_registration_leaves_no_partial_surface(modmod):
    name, module = modmod

    class Broken:
        def register_routers(self, router: APIRouter):
            @router.get("/mounted-before-the-failure")
            def _handler():
                return {"ok": True}

            raise RuntimeError("boom")

    module.Broken = Broken()
    module.Clean = _mod_with_route("/ping", {"from": "clean"})

    app = FastAPI()

    @app.get("/core")
    def _core():
        return {"from": "core"}

    result = load_mods(
        app,
        [_manifest("broken", f"{name}:Broken", "/broken"), _manifest("clean", f"{name}:Clean", "/clean")],
    )

    assert result.loaded_ids == ["clean"]
    assert "broken" in result.errors[0] and "boom" in result.errors[0]

    client = TestClient(app)
    assert client.get("/core").json() == {"from": "core"}, "core must still serve"
    assert client.get("/clean/ping").json() == {"from": "clean"}, "the clean mod must still serve"
    assert client.get("/broken/mounted-before-the-failure").status_code == 404, (
        "a route registered before the failure must not remain reachable"
    )


# --- T-035: per-mod log namespace ------------------------------------------
@pytest.mark.tier0
def test_log_namespace_is_derived_from_the_id_and_distinguishes_mods():
    assert mod_logger("crew").name == f"{LOG_NAMESPACE}.crew"
    assert mod_logger("crew").name != mod_logger("other").name
    assert not mod_logger("crew").name.startswith("selfai_ui.routers")


# --- T-036 / T-037: request-time isolation ---------------------------------
@pytest.mark.tier0
def test_an_exception_in_a_mod_route_does_not_take_down_the_api(modmod):
    name, module = modmod
    module.Boom = _mod_with_route("/boom", raises=RuntimeError("kaboom"))
    module.Clean = _mod_with_route("/ping", {"from": "clean"})

    app = FastAPI()

    @app.get("/core")
    def _core():
        return {"from": "core"}

    result = load_mods(
        app,
        [_manifest("boom", f"{name}:Boom", "/boom"), _manifest("clean", f"{name}:Clean", "/clean")],
    )
    assert result.loaded_ids == ["boom", "clean"], result.errors

    client = TestClient(app, raise_server_exceptions=False)

    first = client.get("/boom/boom")
    assert first.status_code == 500
    body = first.text
    assert "kaboom" not in body, "the response must not carry exception text"
    assert "Traceback" not in body and "File \"" not in body, "no stack trace may leak"

    # Everything still works afterwards, including the same route again.
    assert client.get("/core").json() == {"from": "core"}
    assert client.get("/clean/ping").json() == {"from": "clean"}
    assert client.get("/boom/boom").status_code == 500, "no wedged state"


@pytest.mark.tier0
def test_a_deliberate_http_error_is_not_swallowed(modmod):
    """Containing crashes must not turn a mod's intentional 404 into a 500."""
    name, module = modmod
    module.Mod = _mod_with_route("/missing", raises=HTTPException(status_code=404, detail="nope"))

    app = FastAPI()
    load_mods(app, [_manifest("alpha", f"{name}:Mod", "/alpha")])

    response = TestClient(app).get("/alpha/missing")
    assert response.status_code == 404
    assert response.json()["detail"] == "nope"


# --- T-031 / T-032: lifecycle ----------------------------------------------
@pytest.mark.tier0
def test_startup_runs_once_and_shutdown_is_honoured(modmod):
    name, module = modmod
    calls = []

    class Mod:
        def register_lifecycle(self):
            return {
                "startup": lambda: calls.append("up"),
                "shutdown": lambda: calls.append("down"),
            }

    module.Mod = Mod()

    app = FastAPI()
    result = load_mods(app, [_manifest("alpha", f"{name}:Mod")])

    assert calls == ["up"]
    run_shutdown_hooks(result)
    assert calls == ["up", "down"]


@pytest.mark.tier0
def test_a_mod_whose_startup_raises_is_disabled_and_never_shut_down(modmod):
    name, module = modmod
    calls = []

    class Mod:
        def register_lifecycle(self):
            def _startup():
                raise RuntimeError("startup failed")

            return {"startup": _startup, "shutdown": lambda: calls.append("down")}

    module.Mod = Mod()

    app = FastAPI()
    result = load_mods(app, [_manifest("alpha", f"{name}:Mod")])

    assert result.loaded == {}
    run_shutdown_hooks(result)
    assert calls == [], "a mod that never started must not be shut down"


@pytest.mark.tier0
def test_one_shutdown_hook_raising_does_not_block_the_others(modmod):
    name, module = modmod
    calls = []

    def _make(mod_name, explode=False):
        class Mod:
            def register_lifecycle(self):
                def _shutdown():
                    if explode:
                        raise RuntimeError("shutdown failed")
                    calls.append(mod_name)

                return {"shutdown": _shutdown}

        return Mod()

    module.A = _make("a", explode=True)
    module.B = _make("b")

    app = FastAPI()
    result = load_mods(app, [_manifest("a", f"{name}:A"), _manifest("b", f"{name}:B")])
    assert result.loaded_ids == ["a", "b"]

    run_shutdown_hooks(result)
    assert calls == ["b"]
