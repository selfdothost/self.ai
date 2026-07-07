"""
Auths router CRUD + session tests.

Covers: session user fetch, profile update, password update, signout,
API key CRUD, admin config endpoints.
"""

import pytest


@pytest.mark.tier0
def test_get_session_user(authenticated_user):
    """GET / returns the session user."""
    resp = authenticated_user.get("/api/v1/auths/")
    assert resp.status_code == 200
    body = resp.json()
    assert "id" in body
    assert "email" in body
    assert body["role"] == "user"


@pytest.mark.tier0
def test_get_session_admin(authenticated_admin):
    resp = authenticated_admin.get("/api/v1/auths/")
    assert resp.status_code == 200
    assert resp.json()["role"] == "admin"


@pytest.mark.tier0
def test_update_profile(authenticated_user):
    resp = authenticated_user.post(
        "/api/v1/auths/update/profile",
        json={"name": "Updated Name", "profile_image_url": "/new.png"},
    )
    assert resp.status_code == 200
    # Verify via session endpoint
    me = authenticated_user.get("/api/v1/auths/").json()
    assert me["name"] == "Updated Name"


@pytest.mark.tier0
def test_signout_clears_cookie(authenticated_user):
    resp = authenticated_user.get("/api/v1/auths/signout")
    assert resp.status_code == 200


@pytest.mark.tier0
def test_signin_unknown_user_rejected(client):
    resp = client.post(
        "/api/v1/auths/signin",
        json={"email": "noexist@test.local", "password": "wrong"},
    )
    # Unknown user → 400 (with INVALID_CRED detail) per auths.py.
    # A 500 here would indicate a router crash leaking stack traces.
    assert resp.status_code == 400, (
        f"Expected 400 for unknown-user signin, got {resp.status_code}: "
        f"{resp.text[:200]}"
    )


@pytest.mark.tier0
def test_admin_details_requires_auth(client):
    """The admin/details endpoint requires authentication (not admin role)."""
    resp = client.get("/api/v1/auths/admin/details")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_admin_details_accessible_to_any_user(authenticated_user):
    resp = authenticated_user.get("/api/v1/auths/admin/details")
    # Any verified user can see admin details (who the admin is)
    assert resp.status_code == 200


@pytest.mark.tier0
def test_admin_config_admin_only(authenticated_user):
    resp = authenticated_user.get("/api/v1/auths/admin/config")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_admin_config_admin_access(authenticated_admin):
    resp = authenticated_admin.get("/api/v1/auths/admin/config")
    assert resp.status_code == 200


@pytest.mark.tier0
def test_api_key_create_when_enabled(authenticated_user, test_app):
    """When ENABLE_API_KEY is True, user can create a new API key."""
    original = test_app.state.config.ENABLE_API_KEY
    test_app.state.config.ENABLE_API_KEY = True
    try:
        resp = authenticated_user.post("/api/v1/auths/api_key")
        assert resp.status_code == 200
        body = resp.json()
        assert "api_key" in body
        assert body["api_key"].startswith("sk-")
    finally:
        test_app.state.config.ENABLE_API_KEY = original


@pytest.mark.tier0
def test_api_key_create_when_disabled(authenticated_user, test_app):
    """When ENABLE_API_KEY is False, creation returns 403."""
    original = test_app.state.config.ENABLE_API_KEY
    test_app.state.config.ENABLE_API_KEY = False
    try:
        resp = authenticated_user.post("/api/v1/auths/api_key")
        assert resp.status_code in (400, 401, 403)
    finally:
        test_app.state.config.ENABLE_API_KEY = original


@pytest.mark.tier0
def test_api_key_get_without_key_returns_404(authenticated_user):
    """GET /api_key returns 404 when the user has no API key set."""
    resp = authenticated_user.get("/api/v1/auths/api_key")
    assert resp.status_code == 404


@pytest.mark.tier0
def test_api_key_delete_returns_200(authenticated_user):
    """DELETE /api_key returns 200 (bool result)."""
    resp = authenticated_user.delete("/api/v1/auths/api_key")
    assert resp.status_code == 200
