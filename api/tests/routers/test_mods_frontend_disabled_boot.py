"""Disabled mod -> zero frontend surface, proven through the REAL boot.

T-A04 (frontend-api R3 AC5) and T-A05 (R4 AC5) both require that when a mod is
DISABLED, every request under its per-mod frontend surfaces -- the asset prefix
(`/static/mods/<id>/...`) and the per-mod manifest endpoint
(`/api/v1/mods/<id>/frontend-manifest`) -- returns the same not-found as an
unrouted path, for admin AND non-admin callers alike. This file proves that
against the REAL installed `reference` mod through the REAL boot path, in the
clean-disable style `test_reference_disable_clean.py` (R6) established.

Why a wire-level 404 assertion IS safe here, where `test_reference_disable_clean`
had to avoid it: that file's route-cache caveat is specific to the `/reference`
route the reference mod MOUNTS at boot and its teardown REMOVES by reassigning
`app.router.routes` (never bumping FastAPI's route-version counter, so a stale
match lingers). The two surfaces here are PERMANENT, app-level routes registered
once at import (`main.py`: the asset router and the frontend-manifest router) and
never added or removed per boot -- so no route-version cache is ever stale for
them. Disabling the mod does not remove a route; it makes `_loaded_mod` return
`None` for the mod, which the handlers turn into a 404. The wire status is
therefore a faithful, order-independent signal of the zero-surface posture.

The disabled boot is built directly (empty enabled list) exactly as that file
does: point `config.MODS_INSTALL_DIRS` at the real `api/mods/` root and set an
EMPTY `config.ENABLED_MODS`, so `discover()` never loads the mod and none of its
hooks run. An enabled control boot proves the surfaces are non-vacuous.

Cavekit: cavekit-mods-frontend-api.md R3 AC5 -- T-A04; R4 AC5, AC6 -- T-A05.
Cross-refs: cavekit-mods-discovery.md R2 (zero surface when disabled);
cavekit-mods-reference-implementation.md R6 (the clean-disable pattern reused).
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from selfai_ui.mods.assets import discover_bundle_entry
from tests.mods_reference_boot import (
    REFERENCE_MOD_ID,
    REFERENCE_MODS_DIR,
    REFERENCE_NAMESPACE,
    REFERENCE_PREFIX,
    ensure_reference_table,
)

REFERENCE_ASSET_PREFIX = f"/static/mods/{REFERENCE_MOD_ID}"
REFERENCE_FRONTEND_MANIFEST = f"/api/v1/mods/{REFERENCE_MOD_ID}/frontend-manifest"

#: A mod id that is never installed -- the "unrouted"/never-existed baseline every
#: zero-surface response must be byte-identical to.
GHOST_MOD_ID = "ghost-never-installed"


@contextmanager
def _boot(app, engine, *, enabled: bool):
    """Drive the REAL app through its lifespan with the reference mod enabled or
    disabled, then tear down the process-global state a boot mutates.

    Mirrors `test_reference_disable_clean._boot` exactly: config levers before the
    client is entered; the `/reference` sio namespace and mounted routes removed
    after, so a later boot in the session is clean. When disabled the enabled list
    is EMPTY, so the mod on disk is never loaded and its hooks never run.
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
        sio.handlers.pop(REFERENCE_NAMESPACE, None)
        app.router.routes = [r for r in app.router.routes if not getattr(r, "path", "").startswith(REFERENCE_PREFIX)]


# =============================================================================
# T-A04 non-vacuity control -- the asset prefix is LIVE when the mod is enabled.
# =============================================================================
@pytest.mark.tier1
def test_enabled_reference_asset_prefix_serves_a_real_file(test_app, test_engine):
    """With the reference mod enabled, a real file in its retained directory is
    served under its asset prefix -- so the disabled 404s below mean 'switched
    off', not 'route absent'.

    The probe is the mod's built bundle entry, not its `mod.yaml`. The surface
    serves web-asset file types only (#70), so a manifest is a 404 whether the
    mod is enabled or not -- which would have quietly made this non-vacuity
    control vacuous. Discovered off disk rather than hardcoded, since the
    filename carries a content hash that changes with every build.
    """
    mod_directory = REFERENCE_MODS_DIR / REFERENCE_MOD_ID
    entry = discover_bundle_entry(mod_directory)
    assert entry is not None, "the reference mod must ship a built entry.<hash>.js for this control"
    on_disk = (mod_directory / entry).read_bytes()

    with _boot(test_app, test_engine, enabled=True) as client:
        assert REFERENCE_MOD_ID in test_app.state.MODS.loaded
        resp = client.get(f"{REFERENCE_ASSET_PREFIX}/{entry}")
        assert resp.status_code == 200
        assert resp.content == on_disk

        # The other half of #70, asserted where the surface is really mounted:
        # the mod's own source and manifest are NOT reachable, though they sit in
        # the very directory the bundle was just served from.
        for private in ("mod.yaml", "reference_mod.py", "state.py"):
            refused = client.get(f"{REFERENCE_ASSET_PREFIX}/{private}")
            assert refused.status_code == 404, (
                f"{private} is in the mod's install directory but is not a web asset; "
                f"serving it publishes a mod's source and config"
            )


# =============================================================================
# T-A04 (R3 AC5) -- disabled: every asset request is unrouted-not-found, admin AND
#                   non-admin, matching the never-existed baseline.
# =============================================================================
@pytest.mark.tier1
def test_disabled_reference_asset_prefix_is_not_found_for_admin_and_non_admin(
    test_app, test_engine, test_admin, test_user
):
    asset_paths = ["mod.yaml", "index.html", "entry.deadbeef.js", "assets/app.js"]
    with _boot(test_app, test_engine, enabled=False) as client:
        assert REFERENCE_MOD_ID not in test_app.state.MODS.loaded, "the mod must not load when disabled"

        for token in (test_admin["token"], test_user["token"]):
            headers = {"Authorization": f"Bearer {token}"}
            # The never-existed baseline every disabled response must match.
            control = client.get(f"/static/mods/{GHOST_MOD_ID}/mod.yaml", headers=headers)
            assert control.status_code == 404

            for path in asset_paths:
                resp = client.get(f"{REFERENCE_ASSET_PREFIX}/{path}", headers=headers)
                assert resp.status_code == control.status_code == 404
                assert resp.json() == control.json(), "disabled asset must be indistinguishable from never-existed"


# =============================================================================
# T-A05 non-vacuity -- enabled reference reports the NAMED no_bundle condition
#                      (R4 AC6, through the real mod: it declares a frontend but
#                      ships no built bundle until T-C08), with no-cache headers.
# =============================================================================
@pytest.mark.tier1
def test_enabled_reference_frontend_manifest_reports_the_real_built_bundle(
    test_app, test_engine, test_user
):
    """T-C08 compiled the reference mod's real Svelte 5 custom-element bundle
    (`api/mods/reference/entry.<hash>.js`), so the live manifest now reports
    `status: "ok"` with a real, resolvable `bundle_url` -- exactly what T-A05's
    own docstring predicted ("T-C08 will produce the bundle; then this same
    endpoint reports 'ok'."). The `no_bundle` named condition this test used to
    prove against the reference mod is still covered, generically, by
    `test_mods_frontend_manifest.py::test_the_no_bundle_condition_is_distinguishable_from_disabled_and_from_served`
    (a synthetic mod with no built entry) -- so this real-boot test now proves
    the served-bundle path instead, keeping both sides of R4 AC6 covered."""
    # The endpoint requires a verified user (#70); an ordinary one is enough,
    # since it is not scope-filtered.
    auth = {"Authorization": f"Bearer {test_user['token']}"}
    with _boot(test_app, test_engine, enabled=True) as client:
        assert REFERENCE_MOD_ID in test_app.state.MODS.loaded
        resp = client.get(REFERENCE_FRONTEND_MANIFEST, headers=auth)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        assert body["mod_id"] == REFERENCE_MOD_ID
        assert body["tag"] == "mod-reference"
        assert body["bundle_url"] is not None
        assert body["bundle_url"].startswith(REFERENCE_ASSET_PREFIX)
        # The manifest resolves to a real, byte-servable asset (R4 AC1).
        asset_resp = client.get(body["bundle_url"])
        assert asset_resp.status_code == 200, asset_resp.text
        assert b"mod-reference" in asset_resp.content
        # Always-fresh pointer, never the immutable directive.
        cache_control = resp.headers["cache-control"]
        assert "no-cache" in cache_control
        assert "immutable" not in cache_control


# =============================================================================
# T-A05 (R4 AC5) -- disabled: the per-mod manifest endpoint is unrouted-not-found,
#                   admin AND non-admin, matching the never-existed baseline.
# =============================================================================
@pytest.mark.tier1
def test_disabled_reference_frontend_manifest_is_not_found_for_admin_and_non_admin(
    test_app, test_engine, test_admin, test_user
):
    with _boot(test_app, test_engine, enabled=False) as client:
        assert REFERENCE_MOD_ID not in test_app.state.MODS.loaded

        for token in (test_admin["token"], test_user["token"]):
            headers = {"Authorization": f"Bearer {token}"}
            control = client.get(f"/api/v1/mods/{GHOST_MOD_ID}/frontend-manifest", headers=headers)
            resp = client.get(REFERENCE_FRONTEND_MANIFEST, headers=headers)

            assert control.status_code == 404
            assert resp.status_code == control.status_code == 404
            assert resp.json() == control.json() == {"detail": "Not Found"}
