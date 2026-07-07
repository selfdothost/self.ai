"""
Users router CRUD and profile tests.

User admin endpoints (list, role update, delete) plus session-user
endpoints (settings, info).
"""

import pytest


@pytest.mark.tier0
def test_list_users_admin_only(authenticated_user):
    resp = authenticated_user.get("/api/v1/users/")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_list_users_admin_returns_user_records(authenticated_admin):
    resp = authenticated_admin.get("/api/v1/users/")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    # Admin + their own record should exist
    assert len(body) >= 1


@pytest.mark.tier0
def test_get_user_settings(authenticated_user):
    resp = authenticated_user.get("/api/v1/users/user/settings")
    # May be null initially
    assert resp.status_code == 200


@pytest.mark.tier0
def test_update_user_settings(authenticated_user):
    resp = authenticated_user.post(
        "/api/v1/users/user/settings/update",
        json={"ui": {"theme": "dark"}},
    )
    assert resp.status_code == 200
    # Settings should now include the ui key
    refetch = authenticated_user.get("/api/v1/users/user/settings").json()
    assert refetch is not None
    assert "ui" in refetch or refetch.get("ui") is not None


@pytest.mark.tier0
def test_get_user_info(authenticated_user):
    resp = authenticated_user.get("/api/v1/users/user/info")
    assert resp.status_code == 200


@pytest.mark.tier0
def test_update_user_info(authenticated_user):
    resp = authenticated_user.post(
        "/api/v1/users/user/info/update",
        json={"location": "Seattle"},
    )
    # Endpoint accepts raw dict; returns 200 with merged info
    assert resp.status_code == 200


@pytest.mark.tier0
def test_get_user_by_id(authenticated_user, test_user):
    """A user can fetch basic info about themselves via /{user_id}.
    Endpoint returns only public-profile fields (name, profile_image_url),
    not id/email/role."""
    resp = authenticated_user.get(f"/api/v1/users/{test_user['id']}")
    assert resp.status_code == 200
    body = resp.json()
    # Router populates name + profile_image_url per users.py:224-230
    assert "name" in body
    assert "profile_image_url" in body


@pytest.mark.tier0
def test_get_groups_returns_user_groups(authenticated_user):
    """Returns groups the user is a member of."""
    resp = authenticated_user.get("/api/v1/users/groups")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.tier0
def test_get_permissions(authenticated_user):
    """Returns the user's permission groups."""
    resp = authenticated_user.get("/api/v1/users/permissions")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.tier0
def test_get_default_permissions_admin_only(authenticated_user):
    resp = authenticated_user.get("/api/v1/users/default/permissions")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_get_default_permissions_admin_access(authenticated_admin):
    resp = authenticated_admin.get("/api/v1/users/default/permissions")
    assert resp.status_code == 200


@pytest.mark.tier0
def test_update_role_admin_only(authenticated_user, test_user):
    resp = authenticated_user.post(
        "/api/v1/users/update/role",
        json={"id": test_user["id"], "role": "admin"},
    )
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_delete_user_admin_only(authenticated_user, test_user):
    resp = authenticated_user.delete(f"/api/v1/users/{test_user['id']}")
    assert resp.status_code in (401, 403)
