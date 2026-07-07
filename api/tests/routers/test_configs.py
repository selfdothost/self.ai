"""T-315: Configs router tests (banners, import/export, models, suggestions)."""

import pytest


@pytest.mark.tier0
def test_get_banners(authenticated_user):
    """GET banners is accessible to verified users."""
    resp = authenticated_user.get("/api/v1/configs/banners")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.tier0
def test_set_banners_forbidden_for_user(authenticated_user):
    """Setting banners requires admin — user-role is denied."""
    resp = authenticated_user.post(
        "/api/v1/configs/banners",
        json={"banners": []},
    )
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_set_and_get_banners_round_trip(authenticated_admin):
    authenticated_admin.post(
        "/api/v1/configs/banners",
        json={
            "banners": [
                {
                    "id": "banner1",
                    "type": "info",
                    "title": "Welcome",
                    "content": "Hello world",
                    "dismissible": True,
                    "timestamp": 1234567890,
                }
            ]
        },
    )
    resp = authenticated_admin.get("/api/v1/configs/banners")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["title"] == "Welcome"


@pytest.mark.tier0
def test_export_config(authenticated_admin):
    resp = authenticated_admin.get("/api/v1/configs/export")
    assert resp.status_code == 200
    assert isinstance(resp.json(), dict)


@pytest.mark.tier0
def test_import_export_round_trip(authenticated_admin):
    exported = authenticated_admin.get("/api/v1/configs/export").json()
    resp = authenticated_admin.post("/api/v1/configs/import", json={"config": exported})
    # Import may succeed or return 200 with the stored config
    assert resp.status_code == 200


@pytest.mark.tier0
def test_user_cannot_export_config(authenticated_user):
    resp = authenticated_user.get("/api/v1/configs/export")
    assert resp.status_code in (401, 403)
