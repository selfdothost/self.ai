"""The round-trip fix, checked where it actually matters: enforcement.

`test_user_permissions_roundtrip.py` proves keys survive a save. This proves
the surviving keys still *work* -- that `has_permission()` grants on them
afterwards -- and that an unmodelled `mods.*` namespace behaves the same way a
built-in one does. That equivalence is the entire premise of dot-namespaced mod
scopes: if a mod scope cannot survive an admin save and still be enforced, no
mod can rely on one.

Cavekit: cavekit-mods-permissions.md R3, R4 -- T-023, T-024
"""

import pytest

from selfai_ui.utils.access_control import has_permission


def _roundtrip(client):
    """GET the defaults, POST them back unmodified, return the result."""
    before = client.get("/api/v1/users/default/permissions").json()
    response = client.post("/api/v1/users/default/permissions", json=before)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.tier1
def test_a_default_granted_permission_still_enforces_after_a_roundtrip(authenticated_admin, test_app, test_user):
    """T-023: surviving the save is not enough — it has to still grant."""
    original = test_app.state.config.USER_PERMISSIONS
    test_app.state.config.USER_PERMISSIONS = {
        **original,
        "workspace": {**original.get("workspace", {}), "training": True},
        "features": {**original.get("features", {}), "web_browsing": True},
    }
    try:
        after = _roundtrip(authenticated_admin)

        assert after["workspace"]["training"] is True
        assert after["features"]["web_browsing"] is True

        defaults = test_app.state.config.USER_PERMISSIONS
        assert has_permission(test_user["id"], "workspace.training", defaults)
        assert has_permission(test_user["id"], "features.web_browsing", defaults)
    finally:
        test_app.state.config.USER_PERMISSIONS = original


@pytest.mark.tier1
def test_a_new_key_is_not_dropped_for_want_of_a_model_field(authenticated_admin, test_app):
    """The general property. Naming the two known casualties would have left
    the next permission added to die exactly the same way."""
    original = test_app.state.config.USER_PERMISSIONS
    test_app.state.config.USER_PERMISSIONS = {
        **original,
        "some_future_area": {"a_new_thing": True},
    }
    try:
        after = _roundtrip(authenticated_admin)
        assert after["some_future_area"]["a_new_thing"] is True
    finally:
        test_app.state.config.USER_PERMISSIONS = original


@pytest.mark.tier1
def test_a_mod_namespaced_key_survives_and_enforces(authenticated_admin, test_app, test_user):
    """T-024: the premise of dot-namespaced mod scopes, end to end."""
    original = test_app.state.config.USER_PERMISSIONS
    test_app.state.config.USER_PERMISSIONS = {
        **original,
        "mods": {"crew": {"session": {"connect": True, "drive": False}}},
    }
    try:
        after = _roundtrip(authenticated_admin)

        assert after["mods"]["crew"]["session"]["connect"] is True
        assert after["mods"]["crew"]["session"]["drive"] is False

        defaults = test_app.state.config.USER_PERMISSIONS
        # Checked through the same checker core uses for `workspace.models`,
        # with no mods-specific code path involved.
        assert has_permission(test_user["id"], "mods.crew.session.connect", defaults)
        assert not has_permission(test_user["id"], "mods.crew.session.drive", defaults)
        assert not has_permission(test_user["id"], "mods.crew.session.absent", defaults)
        assert not has_permission(test_user["id"], "mods.other.thing", defaults)
    finally:
        test_app.state.config.USER_PERMISSIONS = original


@pytest.mark.tier1
def test_a_post_carrying_an_unmodelled_mods_key_is_not_rejected(authenticated_admin, test_app):
    """Accept-and-preserve, not reject. A client that knows about a mod the
    request model has never heard of must not be turned away."""
    original = test_app.state.config.USER_PERMISSIONS
    try:
        payload = {
            **original,
            "mods": {"newmod": {"thing": {"read": True}}},
        }
        response = authenticated_admin.post("/api/v1/users/default/permissions", json=payload)

        assert response.status_code == 200, response.text
        assert response.json()["mods"]["newmod"]["thing"]["read"] is True
    finally:
        test_app.state.config.USER_PERMISSIONS = original


@pytest.mark.tier1
def test_a_caller_can_still_revoke(authenticated_admin, test_app):
    """Merge must not mean 'writes are ignored'. False has to stick, or an
    admin cannot take a permission away."""
    original = test_app.state.config.USER_PERMISSIONS
    test_app.state.config.USER_PERMISSIONS = {
        **original,
        "mods": {"crew": {"session": {"connect": True}}},
    }
    try:
        payload = {
            **test_app.state.config.USER_PERMISSIONS,
            "mods": {"crew": {"session": {"connect": False}}},
        }
        response = authenticated_admin.post("/api/v1/users/default/permissions", json=payload)

        assert response.status_code == 200, response.text
        assert response.json()["mods"]["crew"]["session"]["connect"] is False
    finally:
        test_app.state.config.USER_PERMISSIONS = original


@pytest.mark.tier1
def test_a_non_boolean_leaf_is_refused(authenticated_admin):
    """`has_permission()` reads a leaf as a flag, so a string leaf would be
    silently truthy and grant access nobody intended."""
    before = authenticated_admin.get("/api/v1/users/default/permissions").json()
    payload = {**before, "mods": {"crew": {"session": {"connect": "yes"}}}}

    response = authenticated_admin.post("/api/v1/users/default/permissions", json=payload)

    assert response.status_code == 400, response.text
    assert "connect" in response.json()["detail"]
