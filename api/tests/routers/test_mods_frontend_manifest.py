"""Per-mod frontend manifest endpoint: resolution, freshness, and named states.

The endpoint (`routers.mod_frontend_manifest`) is tested through a mounted router
reading a synthetic `app.state.MODS`, mirroring `test_mods_assets.py::_client` --
no real boot needed for the resolution/freshness/shape facts. R4 AC1's "resolves
to a real R3 asset" is proven by mounting BOTH this router and T-A03's asset
router over the same directory and following the URL the manifest hands back. The
disabled-mod zero-surface fact (R4 AC5) is proven here at unit level AND through
the real boot in `test_mods_frontend_disabled_boot.py`; AC6's no-bundle condition
is proven here AND against the real reference mod in that same boot file.

Every fact is read through the surface under test (the endpoint's status, body,
and headers), never by re-implementing discovery.

Cavekit: cavekit-mods-frontend-api.md R4 -- T-A05
"""

import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from selfai_ui.routers import mod_assets, mod_frontend_manifest
from selfai_ui.utils.auth import get_verified_user


# --- fixtures ---------------------------------------------------------------
@pytest.fixture
def mod_dir(tmp_path):
    """A mod directory holding one content-hashed bundle entry at its root."""
    d = tmp_path / "mods" / "widget"
    d.mkdir(parents=True)
    (d / "entry.a1b2c3.js").write_bytes(b"export default class extends HTMLElement {}\n")
    return d


def _loaded(mods):
    """Build a LoadResult-shaped stub. `mods` maps id -> (directory, has_frontend, tag)."""
    loaded = {}
    for mid, (directory, has_frontend, tag) in mods.items():
        frontend = types.SimpleNamespace() if has_frontend else None
        manifest = types.SimpleNamespace(id=mid, frontend=frontend, custom_element_tag=lambda t=tag: t)
        loaded[mid] = types.SimpleNamespace(manifest=manifest, directory=directory)
    return types.SimpleNamespace(loaded=loaded)


def _client(mods, *, with_assets=False, authenticated=True):
    app = FastAPI()
    app.include_router(mod_frontend_manifest.router)
    if with_assets:
        app.include_router(mod_assets.router)
    app.state.MODS = _loaded(mods)
    if authenticated:
        # The manifest endpoint requires a verified user (#70). These cases are
        # about resolution behaviour, not auth, so the dependency is satisfied
        # here rather than threaded through each one. The auth requirement
        # itself is asserted separately, with `authenticated=False`.
        app.dependency_overrides[get_verified_user] = lambda: types.SimpleNamespace(id="u1", role="user")
    return TestClient(app)


# =============================================================================
# R4 AC1 -- GET returns JSON naming the current bundle URL, which resolves to a
#           real asset served by T-A03's surface.
# =============================================================================
@pytest.mark.tier1
def test_manifest_names_the_current_bundle_url_and_it_resolves_to_a_real_asset(mod_dir):
    client = _client({"widget": (mod_dir, True, "mod-widget")}, with_assets=True)

    resp = client.get("/api/v1/mods/widget/frontend-manifest")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["bundle_url"] == "/static/mods/widget/entry.a1b2c3.js"

    # The URL the manifest handed back must resolve to a real asset via T-A03.
    served = client.get(body["bundle_url"])
    assert served.status_code == 200
    assert served.content == b"export default class extends HTMLElement {}\n"


# =============================================================================
# R4 AC2 -- the response carries no-cache / revalidating headers.
# =============================================================================
@pytest.mark.tier1
def test_the_manifest_response_carries_no_cache_headers(mod_dir):
    client = _client({"widget": (mod_dir, True, "mod-widget")})

    resp = client.get("/api/v1/mods/widget/frontend-manifest")

    cache_control = resp.headers["cache-control"]
    assert "no-cache" in cache_control
    assert "must-revalidate" in cache_control
    # It is the always-fresh pointer, NOT the immutable asset -- it must never
    # carry the long-lived immutable directive the asset surface uses.
    assert "immutable" not in cache_control
    assert "max-age=31536000" not in cache_control


# =============================================================================
# R4 AC3 -- the URL is content-hashed/immutable: an update changes the URL in the
#           manifest, and the old URL is not silently overwritten in place.
# =============================================================================
@pytest.mark.tier1
def test_a_bundle_update_changes_the_url_and_does_not_overwrite_in_place(tmp_path):
    d = tmp_path / "mods" / "widget"
    d.mkdir(parents=True)
    old = d / "entry.aaa111.js"
    old.write_bytes(b"v1\n")
    client = _client({"widget": (d, True, "mod-widget")})

    first = client.get("/api/v1/mods/widget/frontend-manifest").json()["bundle_url"]
    assert first == "/static/mods/widget/entry.aaa111.js"

    # A build replaces the entry: a NEW content hash means a NEW filename, and the
    # old file is gone -- bytes were not overwritten under the same URL.
    old.unlink()
    (d / "entry.bbb222.js").write_bytes(b"v2\n")

    second = client.get("/api/v1/mods/widget/frontend-manifest").json()["bundle_url"]
    assert second == "/static/mods/widget/entry.bbb222.js"
    assert second != first
    assert not old.exists(), "the old hashed file is replaced by a new name, not overwritten in place"


# =============================================================================
# R4 AC4 -- not the registry object, not mod.yaml; carries no config, no secrets.
# =============================================================================
@pytest.mark.tier1
def test_the_payload_is_purpose_built_and_carries_no_config_or_secrets(mod_dir):
    client = _client({"widget": (mod_dir, True, "mod-widget")})

    body = client.get("/api/v1/mods/widget/frontend-manifest").json()

    # A fixed whitelist of loader-relevant keys -- nothing else can leak through.
    assert set(body) == {"mod_id", "tag", "status", "bundle_url"}
    # None of the registry entry's fields (name, scopes, view/label/icon/add_to_nav)
    # and no config/secret container appear -- this is a third, distinct object.
    for forbidden in ("name", "scopes", "config", "view", "label", "icon", "add_to_nav", "secret"):
        assert forbidden not in body


# =============================================================================
# R4 AC5 -- a disabled/unknown mod returns the same not-found as an unrouted path.
# =============================================================================
@pytest.mark.tier1
def test_a_mod_not_in_the_loaded_set_is_a_zero_surface_not_found(mod_dir):
    client = _client({"widget": (mod_dir, True, "mod-widget")})

    # A mod id that was never loaded (the disabled/unknown shape) and a second,
    # different never-loaded id must be byte-identical not-founds -- disclosing
    # nothing about which mods exist.
    disabled = client.get("/api/v1/mods/ghost/frontend-manifest")
    unrouted = client.get("/api/v1/mods/never-installed/frontend-manifest")

    assert disabled.status_code == 404
    assert unrouted.status_code == 404
    assert disabled.json() == unrouted.json() == {"detail": "Not Found"}


@pytest.mark.tier1
def test_a_loaded_mod_with_no_frontend_block_is_also_a_zero_surface_not_found(mod_dir):
    """A mod that loaded but declares no frontend surface has no manifest to
    resolve -- same zero-surface not-found, not an error that reveals it exists."""
    client = _client({"widget": (mod_dir, False, None)})
    resp = client.get("/api/v1/mods/widget/frontend-manifest")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}


# =============================================================================
# R4 AC6 -- a frontend mod shipping no built bundle is a distinguishable, named
#           condition -- NOT a 200 pointing at a missing file.
# =============================================================================
@pytest.mark.tier1
def test_a_frontend_mod_with_no_built_bundle_is_a_named_condition(tmp_path):
    empty = tmp_path / "mods" / "widget"
    empty.mkdir(parents=True)  # declares a frontend, but ships no entry.*.js
    client = _client({"widget": (empty, True, "mod-widget")})

    resp = client.get("/api/v1/mods/widget/frontend-manifest")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Distinguishable and NAMED -- not a 200 whose bundle_url points at a file
    # that does not exist.
    assert body["status"] == "no_bundle"
    assert body["bundle_url"] is None


@pytest.mark.tier1
def test_the_no_bundle_condition_is_distinguishable_from_disabled_and_from_served(tmp_path):
    """The three states the endpoint reports are mutually distinguishable:
    served (200 + url), no-bundle (200 + named status + null url), disabled (404)."""
    served_dir = tmp_path / "served"
    served_dir.mkdir()
    (served_dir / "entry.f00d.js").write_bytes(b"x\n")
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    client = _client(
        {"served": (served_dir, True, "mod-served"), "empty": (empty_dir, True, "mod-empty")},
    )

    served = client.get("/api/v1/mods/served/frontend-manifest")
    no_bundle = client.get("/api/v1/mods/empty/frontend-manifest")
    disabled = client.get("/api/v1/mods/gone/frontend-manifest")

    assert served.status_code == 200 and served.json()["status"] == "ok"
    assert served.json()["bundle_url"] == "/static/mods/served/entry.f00d.js"
    assert no_bundle.status_code == 200 and no_bundle.json()["status"] == "no_bundle"
    assert no_bundle.json()["bundle_url"] is None
    assert disabled.status_code == 404
    # No two of the three are the same response.
    assert served.json() != no_bundle.json()
    assert no_bundle.json() != disabled.json()


# =============================================================================
# #70 -- the endpoint is not anonymous.
# =============================================================================
@pytest.mark.tier1
def test_the_manifest_endpoint_requires_an_authenticated_user(mod_dir):
    """It shipped anonymous, which made it a 404-vs-200 oracle over any mod id.

    An unauthenticated caller could enumerate which mods an instance runs, their
    custom-element tags, and their bundle hashes -- verified against production
    with a plain curl from outside the cluster before this was fixed.

    The refusal must NOT be the zero-surface 404: "who are you" and "no such
    mod" are different answers, and collapsing them would make an authenticated
    caller's 404 ambiguous.
    """
    client = _client({"widget": (mod_dir, True, "mod-widget")}, authenticated=False)

    resp = client.get("/api/v1/mods/widget/frontend-manifest")
    assert resp.status_code in (401, 403), (
        f"expected an auth refusal, got {resp.status_code}: {resp.text}"
    )
