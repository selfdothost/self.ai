"""The mod registry: visibility, filtering, response shape, granted scopes.

The pure filtering logic in `mods.registry` is tested directly with stub users
and manifests. The endpoint in `routers.mods` is tested through a mounted router
that reads a synthetic `app.state.MODS` so the tests need no real boot.

Cavekit: cavekit-mods-registry.md R1-R5 -- T-050..T-057
"""

import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from selfai_ui.mods.manifest import ModManifest
from selfai_ui.mods.registry import entry_for, granted_scopes_for, visible_mods

BASE = {"name": "Crew", "version": "0.1.0", "entrypoint": "crew_mod:Mod", "min_core_version": "0.5.0"}


def _manifest(mod_id, scopes=None, frontend=None):
    data = {**BASE, "id": mod_id, "name": mod_id.title()}
    if scopes is not None:
        data["scopes"] = [{"id": s, "desc": s} for s in scopes]
    if frontend is not None:
        # A frontend block is now a full nav-registration shape (frontend-api
        # R1); the registry contract still only reports `bundle_url` (Phase 0
        # R4) -- the nav fields are exercised by the frontend-api R5 task.
        data["frontend"] = {
            "bundle_url": frontend,
            "view": f"{mod_id}-home",
            "label": mod_id.title(),
            "icon": "puzzle",
            "add_to_nav": True,
            "scopes": [f"mods.{mod_id}.view"],
        }
    return ModManifest(**data)


def _user(role="user", uid="u1"):
    return types.SimpleNamespace(id=uid, role=role)


def _checker(granted):
    """A permission checker that grants exactly the dotted keys in `granted`."""

    def _has(uid, key, defaults):
        # Walk the defaults tree like the real checker, then let an explicit
        # grant override. For these tests defaults are usually empty; the point
        # is that visibility follows `granted`.
        node = defaults or {}
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return key in granted
            node = node[part]
        return bool(node) or key in granted

    return _has


CREW = _manifest("crew", scopes=["mods.crew.session.connect", "mods.crew.session.drive"])
OTHER = _manifest("other", scopes=["mods.other.thing.read"], frontend="/static/mods/other/index.html")


# --- T-050 / T-051: what is enabled and loaded ------------------------------
@pytest.mark.tier0
def test_an_admin_sees_every_loaded_mod():
    result = visible_mods(
        _user("admin"), [CREW, OTHER], has_permission=_checker(set()), defaults={}
    )
    assert {e["id"] for e in result} == {"crew", "other"}


@pytest.mark.tier0
def test_disabled_and_failed_mods_are_absent_because_they_are_not_loaded():
    """The registry only ever sees loaded mods -- a disabled or failed mod never
    reaches this code. Tested by omission: pass only the loaded set."""
    result = visible_mods(_user("admin"), [CREW], has_permission=_checker(set()), defaults={})
    assert [e["id"] for e in result] == ["crew"]


@pytest.mark.tier0
def test_with_no_mods_loaded_the_response_is_empty():
    assert visible_mods(_user("admin"), [], has_permission=_checker(set()), defaults={}) == []


# --- T-052 / T-053: non-admin response filtering ---------------------------
@pytest.mark.tier0
def test_a_non_admin_holding_no_scope_sees_no_entry_for_that_mod():
    result = visible_mods(
        _user("user"), [CREW, OTHER], has_permission=_checker(set()), defaults={}
    )
    assert result == []


@pytest.mark.tier0
def test_no_trace_of_an_unheld_mod_appears_anywhere_in_the_payload():
    result = visible_mods(
        _user("user"), [CREW, OTHER], has_permission=_checker(set()), defaults={}
    )
    flat = str(result)
    for leaked in ("crew", "Crew", "other", "Other", "bundle", "/static/mods"):
        assert leaked not in flat


@pytest.mark.tier0
def test_a_partial_holder_sees_the_mod_with_only_the_held_scopes():
    result = visible_mods(
        _user("user"),
        [CREW],
        has_permission=_checker({"mods.crew.session.connect"}),
        defaults={},
    )
    assert len(result) == 1
    assert result[0]["id"] == "crew"
    assert result[0]["scopes"] == ["mods.crew.session.connect"]


@pytest.mark.tier0
def test_two_non_admins_with_different_grants_get_different_sets():
    a = visible_mods(
        _user("user", "a"), [CREW, OTHER], has_permission=_checker({"mods.crew.session.connect"}), defaults={}
    )
    b = visible_mods(
        _user("user", "b"), [CREW, OTHER], has_permission=_checker({"mods.other.thing.read"}), defaults={}
    )
    assert {e["id"] for e in a} == {"crew"}
    assert {e["id"] for e in b} == {"other"}


@pytest.mark.tier0
def test_revoking_the_last_scope_removes_the_mod_from_the_response():
    checker = _checker({"mods.crew.session.connect"})

    first = visible_mods(_user("user"), [CREW], has_permission=checker, defaults={})
    assert {e["id"] for e in first} == {"crew"}

    second = visible_mods(_user("user"), [CREW], has_permission=_checker(set()), defaults={})
    assert second == []


@pytest.mark.tier0
def test_there_are_no_placeholder_entries():
    """Length equals the visible count; ordering reveals no gaps."""
    result = visible_mods(
        _user("user"),
        [CREW, OTHER],
        has_permission=_checker({"mods.crew.session.connect", "mods.other.thing.read"}),
        defaults={},
    )
    assert len(result) == 2
    assert all(e["id"] for e in result)


# --- T-054: admin exemption ------------------------------------------------
@pytest.mark.tier0
def test_an_admin_holding_no_scopes_still_sees_every_loaded_mod():
    result = visible_mods(
        _user("admin"), [CREW, OTHER], has_permission=_checker(set()), defaults={}
    )
    assert {e["id"] for e in result} == {"crew", "other"}


@pytest.mark.tier0
def test_demoting_an_admin_causes_filtering():
    admin_view = visible_mods(_user("admin"), [CREW], has_permission=_checker(set()), defaults={})
    demoted_view = visible_mods(_user("user"), [CREW], has_permission=_checker(set()), defaults={})

    assert {e["id"] for e in admin_view} == {"crew"}
    assert demoted_view == []


# --- T-055: response shape -------------------------------------------------
@pytest.mark.tier0
def test_every_entry_carries_id_and_name():
    result = visible_mods(_user("admin"), [CREW], has_permission=_checker(set()), defaults={})
    assert set(result[0]) >= {"id", "name"}


@pytest.mark.tier0
def test_a_mod_with_a_frontend_carries_bundle_url_and_one_without_does_not():
    result = visible_mods(
        _user("admin"), [CREW, OTHER], has_permission=_checker(set()), defaults={}
    )
    by_id = {e["id"]: e for e in result}
    assert "bundle_url" in by_id["other"]
    assert by_id["other"]["bundle_url"] == "/static/mods/other/index.html"
    assert "bundle_url" not in by_id["crew"]


@pytest.mark.tier0
def test_the_response_contains_no_configuration_values_or_secrets():
    secret_manifest = _manifest("crew")
    secret_manifest.config = [types.SimpleNamespace(key="api_key", desc="x", secret=True, default=None)]
    result = entry_for(secret_manifest, held_scopes=[])
    assert "config" not in result
    assert "api_key" not in str(result)


# --- frontend-api R5 (T-A06): the nav shape rides the registry entry -------
# OTHER declares a full frontend block (see `_manifest`): bundle_url plus the
# nav fields view/label/icon/add_to_nav. CREW declares no frontend.
_NAV_FIELDS = ("view", "label", "icon", "add_to_nav")


@pytest.mark.tier1
def test_a_frontend_entry_carries_the_nav_fields_alongside_bundle_url():
    """R5 AC1: a mod declaring a frontend block carries view/label/icon/add_to_nav
    IN ADDITION to bundle_url; a mod with no frontend block carries none of them."""
    result = visible_mods(_user("admin"), [CREW, OTHER], has_permission=_checker(set()), defaults={})
    by_id = {e["id"]: e for e in result}

    other = by_id["other"]
    # bundle_url is reported unchanged (Phase 0 R4) ...
    assert other["bundle_url"] == "/static/mods/other/index.html"
    # ... and the nav fields ride alongside it, matching the manifest.
    assert other["view"] == "other-home"
    assert other["label"] == "Other"
    assert other["icon"] == "puzzle"
    assert other["add_to_nav"] is True

    # A mod with no frontend block carries none of the nav fields (nor bundle_url).
    crew = by_id["crew"]
    assert "bundle_url" not in crew
    for field in _NAV_FIELDS:
        assert field not in crew


@pytest.mark.tier1
def test_a_non_admin_without_scope_gets_no_entry_and_therefore_no_nav_fields():
    """R5 AC2: the nav fields ride the existing mod-level scope filter -- a
    non-admin holding no scope for OTHER gets no entry for it at all, so no nav
    fields for it, and no second gating path is introduced."""
    result = visible_mods(_user("user"), [CREW, OTHER], has_permission=_checker(set()), defaults={})
    assert result == []

    # A partial holder of OTHER sees OTHER's nav fields, but nothing of CREW.
    partial = visible_mods(
        _user("user"), [CREW, OTHER], has_permission=_checker({"mods.other.thing.read"}), defaults={}
    )
    assert {e["id"] for e in partial} == {"other"}
    other = partial[0]
    assert other["view"] == "other-home"
    assert other["add_to_nav"] is True
    # No leak of CREW anywhere -- the unheld mod contributes no entry, no fields.
    assert "crew" not in str(partial).lower()


@pytest.mark.tier1
def test_an_admin_sees_the_nav_fields_regardless_of_scopes_held():
    """R5 AC3: an admin holding no scopes still sees the nav fields for every
    enabled, loaded mod that declares a frontend block (admin exemption)."""
    result = visible_mods(_user("admin"), [CREW, OTHER], has_permission=_checker(set()), defaults={})
    other = {e["id"]: e for e in result}["other"]
    for field in _NAV_FIELDS:
        assert field in other, f"admin must see the {field} nav field"
    assert other["view"] == "other-home"
    assert other["label"] == "Other"
    assert other["icon"] == "puzzle"
    assert other["add_to_nav"] is True


@pytest.mark.tier1
def test_the_nav_fields_introduce_no_configuration_values_or_secrets():
    """R5 AC4: adding the nav fields does not introduce any configuration value
    or secret onto the entry -- the response invariant is preserved."""
    secret_frontend = _manifest("other", scopes=["mods.other.thing.read"], frontend="/static/mods/other/index.html")
    secret_frontend.config = [types.SimpleNamespace(key="api_key", desc="x", secret=True, default=None)]
    entry = entry_for(secret_frontend, held_scopes=[])
    # The nav fields are present ...
    for field in _NAV_FIELDS:
        assert field in entry
    # ... but no config key or secret leaked in with them.
    assert "config" not in entry
    assert "api_key" not in str(entry)
    assert entry["bundle_url"] == "/static/mods/other/index.html"


@pytest.mark.tier1
def test_the_nav_fields_do_not_alter_which_mods_are_visible():
    """R5 AC5: the nav fields are additive -- the scope-filtering behaviour the
    existing suite asserts is unchanged. Which mods appear depends only on scopes
    held / admin role, exactly as before; the nav fields ride that decision."""
    admin = visible_mods(_user("admin"), [CREW, OTHER], has_permission=_checker(set()), defaults={})
    assert {e["id"] for e in admin} == {"crew", "other"}

    non_admin = visible_mods(_user("user"), [CREW, OTHER], has_permission=_checker(set()), defaults={})
    assert non_admin == []


# --- T-056 / T-057: granted scopes on each entry ---------------------------
@pytest.mark.tier0
def test_a_held_scope_is_present_and_an_unheld_one_is_absent():
    held = granted_scopes_for(
        CREW, "u1", has_permission=_checker({"mods.crew.session.connect"}), defaults={}
    )
    assert held == ["mods.crew.session.connect"]
    assert "mods.crew.session.drive" not in held


@pytest.mark.tier0
def test_only_the_mods_own_scopes_can_appear():
    """The manifest validator already refused scopes outside mods.<id>, so a
    core key cannot be declared. This pins that the registry trusts that."""
    assert all(s.startswith("mods.crew.") for s in granted_scopes_for(
        CREW, "u1", has_permission=_checker({"mods.crew.session.connect", "workspace.models"}), defaults={}
    ))


@pytest.mark.tier0
def test_an_admin_with_no_personal_grants_reports_an_empty_scope_set():
    """An admin's entry reports their OWN grants, which may be empty. Their
    visibility comes from the admin exemption, not from holding scopes."""
    result = visible_mods(_user("admin"), [CREW], has_permission=_checker(set()), defaults={})
    assert result[0]["scopes"] == []


@pytest.mark.tier0
def test_the_reported_set_uses_the_same_checker_as_enforcement():
    """If the checker and the report disagreed, the entry would list scopes the
    caller cannot actually use. This pins that one function supplies both."""
    checker = _checker({"mods.crew.session.connect"})

    enforced = checker("u1", "mods.crew.session.connect", {})
    reported = "mods.crew.session.connect" in granted_scopes_for(
        CREW, "u1", has_permission=checker, defaults={}
    )
    assert enforced is reported is True


# --- endpoint integration --------------------------------------------------
def _client_with(loaded_manifests, defaults):
    app = FastAPI()
    from selfai_ui.routers import mods as mods_router

    app.include_router(mods_router.router, prefix="/api/v1/mods", tags=["mods"])
    # A LoadResult-shaped stub: the router reads .loaded.values() -> .manifest
    loaded = {
        m.id: types.SimpleNamespace(manifest=m) for m in loaded_manifests
    }
    app.state.MODS = types.SimpleNamespace(loaded=loaded)
    app.state.config = types.SimpleNamespace(USER_PERMISSIONS=defaults)
    return TestClient(app), app


@pytest.mark.tier1
def test_the_endpoint_refuses_an_unauthenticated_request():
    client, _ = _client_with([CREW], {})
    assert client.get("/api/v1/mods/enabled").status_code in {401, 403}


@pytest.mark.tier1
def test_the_endpoint_returns_the_filtered_set_for_an_admin(test_app, authenticated_admin, test_user, db_session):
    """The real router, against the real app state. We publish a synthetic
    MODS set and assert the admin sees it. (test_app already authenticates via
    authenticated_admin.)"""
    from selfai_ui.models.groups import GroupForm, Groups, GroupUpdateForm

    # Publish a loaded crew mod onto the running app.
    loaded = {"crew": types.SimpleNamespace(manifest=CREW)}
    test_app.state.MODS = types.SimpleNamespace(loaded=loaded)

    # Grant the test user one crew scope via a group.
    group = Groups.insert_new_group(test_user["id"], GroupForm(name="r", description="x"))
    Groups.update_group_by_id(
        group.id,
        GroupUpdateForm(
            name=group.name,
            description=group.description,
            permissions={"mods": {"crew": {"session": {"connect": True}}}},
            user_ids=[test_user["id"]],
        ),
    )

    admin_resp = authenticated_admin.get("/api/v1/mods/enabled")
    assert admin_resp.status_code == 200
    assert {e["id"] for e in admin_resp.json()} == {"crew"}, "admin sees the loaded mod"


# --- GET /scopes: full declared-scope list for the permissions editor (self.ai#69) ---
#
# `test_app` is a shared app instance reused across the whole test session, so
# every test here that overwrites `test_app.state.MODS` / `.config.USER_PERMISSIONS`
# MUST restore the original value afterward (mirrors conftest.py's
# `user_without_workspace_permissions` fixture) -- otherwise it leaks into every
# test that runs later in the same session, regardless of which file it's in.


@pytest.mark.tier1
def test_scopes_endpoint_refuses_an_unauthenticated_request():
    client, _ = _client_with([CREW], {})
    assert client.get("/api/v1/mods/scopes").status_code in {401, 403}


@pytest.mark.tier1
def test_scopes_endpoint_refuses_a_non_admin(authenticated_user, test_app):
    original_mods = getattr(test_app.state, "MODS", None)
    try:
        loaded = {"crew": types.SimpleNamespace(manifest=CREW)}
        test_app.state.MODS = types.SimpleNamespace(loaded=loaded)

        resp = authenticated_user.get("/api/v1/mods/scopes")
        assert resp.status_code in {401, 403}
    finally:
        test_app.state.MODS = original_mods


@pytest.mark.tier1
def test_scopes_endpoint_lists_every_declared_scope_with_its_description_regardless_of_grant(
    authenticated_admin, test_app
):
    """The whole point of this endpoint: unlike /enabled, an ungranted scope
    still appears here -- an admin must be able to see and grant it, not just
    confirm scopes already held."""
    original_mods = getattr(test_app.state, "MODS", None)
    original_permissions = test_app.state.config.USER_PERMISSIONS
    try:
        loaded = {"crew": types.SimpleNamespace(manifest=CREW)}
        test_app.state.MODS = types.SimpleNamespace(loaded=loaded)
        test_app.state.config.USER_PERMISSIONS = {}  # no grants anywhere

        resp = authenticated_admin.get("/api/v1/mods/scopes")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["id"] == "crew"
        assert body[0]["name"] == "Crew"
        scope_ids = {s["id"] for s in body[0]["scopes"]}
        assert scope_ids == {"mods.crew.session.connect", "mods.crew.session.drive"}
        # desc is present on every scope -- that's what the toggle label renders
        assert all(s["desc"] for s in body[0]["scopes"])
    finally:
        test_app.state.MODS = original_mods
        test_app.state.config.USER_PERMISSIONS = original_permissions


@pytest.mark.tier1
def test_scopes_endpoint_omits_a_mod_that_declares_no_scopes(authenticated_admin, test_app):
    original_mods = getattr(test_app.state, "MODS", None)
    try:
        no_scopes_mod = _manifest("bare")
        loaded = {"bare": types.SimpleNamespace(manifest=no_scopes_mod)}
        test_app.state.MODS = types.SimpleNamespace(loaded=loaded)

        resp = authenticated_admin.get("/api/v1/mods/scopes")
        assert resp.status_code == 200
        assert resp.json() == []
    finally:
        test_app.state.MODS = original_mods


@pytest.mark.tier1
def test_scopes_endpoint_with_no_mods_loaded_is_empty(authenticated_admin, test_app):
    original_mods = getattr(test_app.state, "MODS", None)
    try:
        test_app.state.MODS = types.SimpleNamespace(loaded={})
        resp = authenticated_admin.get("/api/v1/mods/scopes")
        assert resp.status_code == 200
        assert resp.json() == []
    finally:
        test_app.state.MODS = original_mods
