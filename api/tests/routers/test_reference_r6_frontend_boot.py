"""R6 (T-A08): ONE boot proves the reference mod's UI surface is servable end to
end, through the real surfaces -- the frontend-api analogue of Phase 1's R8/T-016.

This is the Verification Convention (`cavekit-mods-overview.md`) applied literally
to the Phase-2 frontend surfaces, and the direct analogue of
`tests/routers/test_reference_r8_boot.py` (T-016), which proved the *backend*
contract (route, namespace, tool, scope, table) in one real boot. Where R8 drives
the reference mod's backend surfaces through the real `selfai_ui.main.app`
lifespan, this file drives its FRONTEND surfaces -- the per-mod static asset
prefix (R3), the per-mod frontend-manifest endpoint (R4), and the registry's nav
fields (R5) -- through that same real boot, never off `app.state.MODS`, boot-code
output, or files read directly off disk.

Two real boots, matching R6's own criteria:

  * The primary test (`reference_booted_client`, the shared T-009 fixture) boots
    the REAL app with the reference mod ENABLED and, WITHIN THAT ONE BOOT, proves
    R6 AC2, AC3, AC4, and AC6 -- each read through its own caller-facing HTTP
    surface.
  * A second, SEPARATE disabled boot (empty `ENABLED_MODS`, the clean-disable
    style `test_reference_disable_clean.py` / `test_mods_frontend_disabled_boot.py`
    established) proves R6 AC5: with the mod disabled and rebooted, the asset
    prefix and the frontend-manifest endpoint both return not-found and the nav
    fields are absent from every caller's registry response.

R6 AC1 (the reference `mod.yaml` declares a valid `frontend` block) was already
proven by T-A07's own test
(`test_reference_mod.py::test_reference_manifest_declares_a_valid_frontend_nav_block`)
and is not re-proven here.

Route-cache caveat -- confirmed NOT to apply here. `test_reference_disable_clean`
(R6, backend) deliberately avoids a wire-level 404 assertion because the
`/reference` API route the mod MOUNTS at boot is removed at teardown by
reassigning `app.router.routes` without bumping FastAPI's route-version counter,
so a stale match can linger. The three surfaces THIS file asserts on -- the asset
router, the frontend-manifest router, and `/api/v1/mods/enabled` -- are PERMANENT,
app-level routes registered once at import in `main.py`, never added or removed per
boot. Disabling the mod does not remove any route; it makes the loaded-mod lookup
return `None`, which the handlers turn into a 404 and which drops the mod from the
registry response. No route-version cache is ever stale for these routes, so the
wire status is a faithful, order-independent signal of the zero-surface posture --
exactly the reasoning `test_mods_frontend_disabled_boot.py`'s own docstring states,
confirmed to hold for R6's proof too.

Forbidden shortcut (Verification Convention, `cavekit-mods-overview.md`): every
fact below is read through its own real HTTP surface -- the asset prefix, the
frontend-manifest endpoint, the registry endpoint -- through the real application
boot. Nothing is read off `app.state.MODS`, off the boot code's output, or off the
bundle file on disk; the "boot actually ran" guard itself is taken through the
registry endpoint, not off `app.state.MODS`.

Cavekit: cavekit-mods-frontend-api.md R6 -- T-A08 (AC 2,3,4,5,6; AC1 is T-A07).
Cross-refs: R3 (asset prefix), R4 (frontend-manifest), R5 (registry nav fields);
cavekit-mods-reference-implementation.md R6/R8; cavekit-mods-discovery.md R2/R5.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from tests.mods_reference_boot import (
    REFERENCE_MOD_ID,
    REFERENCE_MODS_DIR,
    REFERENCE_NAMESPACE,
    REFERENCE_PREFIX,
)

#: The per-mod frontend surfaces under proof, named once.
REFERENCE_ASSET_PREFIX = f"/static/mods/{REFERENCE_MOD_ID}"
REFERENCE_FRONTEND_MANIFEST = f"/api/v1/mods/{REFERENCE_MOD_ID}/frontend-manifest"
REGISTRY_ENABLED = "/api/v1/mods/enabled"

#: The reference mod's declared nav shape (`api/mods/reference/mod.yaml`) -- the
#: exact values the registry must report for an admin (R5 / R6 AC4).
EXPECTED_NAV = {"view": "reference-home", "label": "Reference", "icon": "puzzle", "add_to_nav": True}

#: The derived custom-element tag (`naming.custom_element_tag_for("reference")`),
#: and the marker its compiled bundle must contain -- the concrete proof the
#: served bytes are the real Svelte 5 component, not a placeholder (R6 AC6).
REFERENCE_TAG = "mod-reference"
BUNDLE_MARKER = b'customElements.define("mod-reference"'

#: A real compiled Svelte 5 custom-element bundle is tens of kilobytes; a stub or
#: placeholder would be a few bytes. The floor guards against a vacuous "served an
#: empty file" pass (R6 AC2 / AC6).
MIN_REAL_BUNDLE_BYTES = 50_000

#: A mod id that is never installed -- the never-existed baseline every disabled
#: response must be byte-identical to (R6 AC5, zero-surface).
GHOST_MOD_ID = "ghost-never-installed"


# =============================================================================
# R6 AC2, AC3, AC4, AC6 -- ONE enabled boot, every fact through its own surface.
# =============================================================================
@pytest.mark.tier2
def test_one_boot_serves_and_resolves_the_reference_frontend_surface(reference_booted_client):
    """The whole servable-end-to-end proof, within a single real boot.

    Read the module docstring for the map of which block proves which criterion.
    The asset prefix and the frontend-manifest endpoint are public surfaces (no
    auth); the registry is read as the fixture's admin.
    """
    boot = reference_booted_client
    public = boot.client  # unauthenticated: the asset + manifest surfaces are public
    admin = boot.as_admin()

    # -- boot-ran guard, THROUGH the registry surface (not app.state.MODS) --------
    # The ONLY thing that ran boot_mods is the fixture's TestClient(app) lifespan.
    # If that boot invocation were removed from startup, the reference mod would be
    # absent from the registry response and every assertion below would fail -- the
    # F-002-shaped guard, taken through a real endpoint rather than off boot output.
    reg = admin.get(REGISTRY_ENABLED)
    assert reg.status_code == 200, reg.text
    entries = {e["id"]: e for e in reg.json()}
    assert REFERENCE_MOD_ID in entries, f"the enabled reference mod is absent from the registry: {sorted(entries)}"

    # -- R6 AC3 (R4), part 1: the frontend-manifest endpoint names a bundle URL ---
    manifest = public.get(REFERENCE_FRONTEND_MANIFEST)
    assert manifest.status_code == 200, manifest.text
    body = manifest.json()
    assert body["status"] == "ok", body
    assert body["mod_id"] == REFERENCE_MOD_ID, body
    assert body["tag"] == REFERENCE_TAG, body
    bundle_url = body["bundle_url"]
    assert bundle_url and bundle_url.startswith(REFERENCE_ASSET_PREFIX), bundle_url
    # It must be an always-fresh pointer, never the immutable asset directive.
    assert "no-cache" in manifest.headers["cache-control"], manifest.headers["cache-control"]

    # -- R6 AC2 + AC6: a request under the per-mod ASSET PREFIX returns the real ---
    #    built bundle bytes. Build the asset URL directly from the prefix + the
    #    live content-hashed filename (resolved dynamically, robust to a rebuild),
    #    so this request is a genuine asset-prefix hit, not a re-read of the same
    #    manifest URL. The bytes are non-trivial and carry the compiled-component
    #    marker -- proving it is T-C08's real Svelte 5 artifact, not a placeholder.
    filename = bundle_url.rsplit("/", 1)[-1]
    direct_asset_url = f"{REFERENCE_ASSET_PREFIX}/{filename}"
    asset = public.get(direct_asset_url)
    assert asset.status_code == 200, asset.text
    served = asset.content
    assert len(served) > MIN_REAL_BUNDLE_BYTES, f"expected a real compiled bundle, got {len(served)} bytes"
    assert BUNDLE_MARKER in served, "served bytes must contain the compiled custom-element marker (not a placeholder)"
    # Content-hashed assets are served immutable (R3 AC4) -- the asset, not the
    # always-fresh manifest above.
    assert "immutable" in asset.headers["cache-control"], asset.headers["cache-control"]

    # -- R6 AC3 (R4), part 2: the manifest's bundle_url RESOLVES to that same ------
    #    served bundle -- fetch the URL the manifest named and confirm identical
    #    bytes to the asset-prefix request above.
    resolved = public.get(bundle_url)
    assert resolved.status_code == 200, resolved.text
    assert resolved.content == served, "the manifest-named URL must resolve to the same served bundle bytes"

    # -- R6 AC4 (R5): the registry reports the nav fields for an admin, read -------
    #    through the endpoint itself (the `entries` fetched above), never off
    #    app.state.MODS or the boot code's output.
    ref = entries[REFERENCE_MOD_ID]
    for field, expected in EXPECTED_NAV.items():
        assert ref.get(field) == expected, f"nav field {field!r}: expected {expected!r}, got {ref.get(field)!r}"


# =============================================================================
# R6 AC5 -- a SEPARATE disabled boot: asset prefix + frontend-manifest not-found,
#           nav fields absent from every caller's registry response.
# =============================================================================
@contextmanager
def _disabled_boot(app, engine):
    """Drive the REAL app through its lifespan with the reference mod DISABLED
    (empty enabled list), then tear down the process-global state a boot mutates.

    Mirrors `test_mods_frontend_disabled_boot._boot(enabled=False)`: config levers
    before the client is entered; the `/reference` sio namespace and any mounted
    routes removed after, so a later boot in the session is clean. With the enabled
    list EMPTY the mod on disk is never loaded and its hooks never run.
    """
    from selfai_ui import config as config_module
    from selfai_ui.socket.main import sio

    orig_dirs = getattr(config_module, "MODS_INSTALL_DIRS", [])
    orig_enabled = list(config_module.ENABLED_MODS.value)

    config_module.MODS_INSTALL_DIRS = [REFERENCE_MODS_DIR]
    config_module.ENABLED_MODS.value = []
    try:
        with TestClient(app) as client:
            yield client
    finally:
        config_module.MODS_INSTALL_DIRS = orig_dirs
        config_module.ENABLED_MODS.value = orig_enabled
        sio.handlers.pop(REFERENCE_NAMESPACE, None)
        app.router.routes = [r for r in app.router.routes if not getattr(r, "path", "").startswith(REFERENCE_PREFIX)]


def _reference_bundle_filename() -> str:
    """The current content-hashed bundle filename on disk, for URL construction
    only. This does NOT read a served fact off disk -- it only builds a URL whose
    HTTP 404 (asserted below) is the actual proof. Falls back to a plausible name
    if the artifact is absent, since a disabled mod 404s any path under its prefix.
    """
    entries = sorted((REFERENCE_MODS_DIR / REFERENCE_MOD_ID).glob("entry.*.js"))
    return entries[0].name if entries else "entry.deadbeef.js"


@pytest.mark.tier2
def test_disabled_reboot_removes_the_whole_frontend_surface_for_every_caller(
    test_app, test_engine, test_admin, test_user
):
    """R6 AC5: with the reference mod DISABLED and rebooted, its asset prefix and
    its frontend-manifest endpoint both return the unrouted not-found, and its nav
    fields are absent from every caller's registry response.

    Every not-found is asserted byte-identical to a never-installed ghost-mod
    control, so 'disabled' is indistinguishable from 'never existed' -- and the
    registry is checked for BOTH an admin and a non-admin, so the nav fields are
    gone for every caller, not merely filtered for one. (The route-cache caveat
    does not apply to these permanent app-level routes; see the module docstring.)
    """
    # The live content-hashed bundle name, so the disabled asset check targets the
    # very artifact the enabled boot served -- proving the whole surface is gone.
    bundle_name = _reference_bundle_filename()
    asset_paths = [bundle_name, "index.html", "assets/app.js"]

    with _disabled_boot(test_app, test_engine) as client:
        # -- asset prefix: 404 for admin, non-admin, AND unauthenticated ----------
        for headers in (
            {"Authorization": f"Bearer {test_admin['token']}"},
            {"Authorization": f"Bearer {test_user['token']}"},
            {},
        ):
            control = client.get(f"/static/mods/{GHOST_MOD_ID}/{bundle_name}", headers=headers)
            assert control.status_code == 404, control.text
            for path in asset_paths:
                resp = client.get(f"{REFERENCE_ASSET_PREFIX}/{path}", headers=headers)
                assert resp.status_code == 404, resp.text
                assert resp.json() == control.json(), "disabled asset must be indistinguishable from never-existed"

        # -- frontend-manifest endpoint: 404, byte-identical to the control ------
        #
        # Authenticated callers only. The endpoint now requires a verified user
        # (#70), so an anonymous caller never reaches the zero-surface question --
        # asserted separately below, because "who are you" and "no such mod" must
        # stay different answers. The asset loop above still covers the anonymous
        # case, since that surface is deliberately open.
        for headers in (
            {"Authorization": f"Bearer {test_admin['token']}"},
            {"Authorization": f"Bearer {test_user['token']}"},
        ):
            manifest_control = client.get(f"/api/v1/mods/{GHOST_MOD_ID}/frontend-manifest", headers=headers)
            manifest = client.get(REFERENCE_FRONTEND_MANIFEST, headers=headers)
            assert manifest_control.status_code == 404, manifest_control.text
            assert manifest.status_code == 404, manifest.text
            assert manifest.json() == manifest_control.json() == {"detail": "Not Found"}

        # An anonymous caller is refused for lacking identity, NOT told the mod is
        # absent -- so the disabled-vs-never-existed guarantee above is a claim
        # about callers who got far enough to ask.
        anonymous = client.get(REFERENCE_FRONTEND_MANIFEST)
        assert anonymous.status_code in (401, 403), anonymous.text

        # -- registry: no reference entry (hence no nav fields) for any caller ----
        for token in (test_admin["token"], test_user["token"]):
            reg = client.get(REGISTRY_ENABLED, headers={"Authorization": f"Bearer {token}"})
            assert reg.status_code == 200, reg.text
            ids = {e["id"] for e in reg.json()}
            assert REFERENCE_MOD_ID not in ids, f"a disabled mod must contribute no registry entry; saw {sorted(ids)}"
            # Belt-and-braces: no entry carries the reference mod's nav view.
            assert all(e.get("view") != EXPECTED_NAV["view"] for e in reg.json()), reg.json()
