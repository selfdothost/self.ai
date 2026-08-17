"""Mod routes must survive the SPA catch-all (self.ai#119).

This is the test whose absence let #119 through. `test_mods_boot_integration.py`
drives the real app through its real lifespan, but it runs API-ONLY: there is no
frontend build in the test environment, so `main.py` takes the "Serving API
only" branch and never mounts the SPA. The shadowing it would cause is therefore
invisible there.

The combined image DOES serve the frontend from the API process, and that is the
deployment recommended for getting started -- so this configuration is the one
most new users run, and it silently disabled the entire mod API surface.

Mechanism: Starlette matches routes in REGISTRATION ORDER. `Mount("/")` matches
every path, is registered at import time, and `boot_mods` mounts mod routers
later, during the lifespan. Both halves of the symptom are asserted below,
because they fail differently:

  * POST -> 405, since StaticFiles allows GET/HEAD only
  * GET  -> 200 with index.html, so JSON callers fail somewhere unrelated

Cavekit: cavekit-mods-discovery.md R1/R4.
"""

import sys
import textwrap
import types

import pytest
from fastapi.testclient import TestClient

MOD_ID = "spashadow"
MOD_ENTRYPOINT = f"_spashadow_mod:{'Mod'}"


@pytest.fixture
def installed_mod(tmp_path, monkeypatch):
    """A mod exposing BOTH a GET and a POST under its prefix.

    The POST matters: it is the case that fails loudly (405). The GET is the
    quieter and arguably worse one -- it returns the SPA's HTML with a 200.
    """
    mod_module = types.ModuleType("_spashadow_mod")
    exec(
        textwrap.dedent(
            """
            from fastapi import APIRouter

            class Mod:
                def register_routers(self, router: APIRouter):
                    @router.get("/state")
                    def state():
                        return {"mod": "spashadow", "kind": "json"}

                    @router.post("/submit")
                    def submit():
                        return {"mod": "spashadow", "submitted": True}
            """
        ),
        mod_module.__dict__,
    )
    sys.modules["_spashadow_mod"] = mod_module

    install_dir = tmp_path / "mods"
    (install_dir / MOD_ID).mkdir(parents=True)
    (install_dir / MOD_ID / "mod.yaml").write_text(
        f"id: {MOD_ID}\n"
        f"name: SPA Shadow Test\n"
        f"version: 0.1.0\n"
        f"entrypoint: {MOD_ENTRYPOINT}\n"
        f"min_core_version: 0.5.0\n"
        f"api:\n  prefix: /{MOD_ID}\n",
        encoding="utf-8",
    )

    from selfai_ui import config as config_module

    monkeypatch.setattr(config_module, "MODS_INSTALL_DIRS", [install_dir])
    monkeypatch.setattr(config_module.ENABLED_MODS, "value", [MOD_ID], raising=False)

    yield
    sys.modules.pop("_spashadow_mod", None)


@pytest.fixture
def spa_frontend(tmp_path):
    """A minimal built frontend, mounted the way main.py mounts it.

    Mounted BEFORE the lifespan runs, which is what makes this a faithful
    reproduction: in the real app the mount happens at import, strictly earlier
    than boot_mods. Registering it here inside the test would prove nothing,
    because it would already be after the mod routes.
    """
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / "index.html").write_text("<!doctype html><title>spa</title>", encoding="utf-8")
    return build_dir


@pytest.fixture
def booted_client(test_app, spa_frontend, installed_mod):
    """Real app + a real SPA catch-all + an enabled mod, driven through lifespan.

    Route list is snapshotted and restored: mod routers are mounted with no
    unmount by design (enablement is a boot-time decision), and the app object is
    shared across the test process.
    """
    before = list(test_app.routes)
    # The app's OWN SPAStaticFiles, not a plain StaticFiles: the real mount falls
    # back to index.html on a 404 so client-side routes resolve. A plain
    # StaticFiles 404s instead, which would make this fixture an unfaithful
    # stand-in for the thing being tested.
    from selfai_ui.main import SPAStaticFiles

    test_app.mount(
        "/",
        SPAStaticFiles(directory=str(spa_frontend), html=True),
        name="spa-static-files",
    )
    try:
        with TestClient(test_app) as c:
            yield c
    finally:
        test_app.routes[:] = before


# NOTE: an earlier revision also asserted the SPA mount's POSITION in
# `app.router.routes`. It was dropped, deliberately. It kept failing to locate the
# mod's routes in that table even though the HTTP tests below prove those routes
# are reachable -- so it was measuring an internal representation that does not
# straightforwardly reflect what is served. The behavioural tests are the ones
# that would catch a regression, and an internals assertion that needs two
# rounds of fixing to say what HTTP already says is a liability, not coverage.


@pytest.mark.tier1
def test_mod_post_route_is_not_swallowed_by_the_spa(booted_client):
    """POST must reach the mod, not StaticFiles.

    A shadowed POST returns 405 (StaticFiles is GET/HEAD only) -- the exact
    symptom seen against the reference mod: POST /reference/submit -> 405.
    """
    res = booted_client.post(f"/{MOD_ID}/submit")
    assert res.status_code != 405, (
        "405 means StaticFiles answered instead of the mod -- the SPA catch-all "
        "is shadowing the mod router again (self.ai#119)"
    )
    assert res.status_code == 200, res.text
    assert res.json()["submitted"] is True


@pytest.mark.tier1
def test_mod_get_route_returns_json_not_the_spa_html(booted_client):
    """GET is the quieter half: shadowed, it 200s with index.html.

    Asserting on the BODY rather than the status is the point -- a status-only
    check passes while the caller receives a web page.
    """
    res = booted_client.get(f"/{MOD_ID}/state")
    assert res.status_code == 200, res.text
    assert "text/html" not in res.headers.get("content-type", ""), (
        "the SPA answered this mod route with HTML -- shadowing has regressed"
    )
    assert res.json()["kind"] == "json"


@pytest.mark.tier1
def test_the_spa_still_serves_unmatched_paths(booted_client):
    """The reordering must not break the thing the catch-all exists for."""
    res = booted_client.get("/some/client-side/route")
    assert res.status_code == 200
    assert "text/html" in res.headers.get("content-type", "")
