"""
T-313: Models router CRUD tests.
"""

import pytest


def _model_payload(model_id: str, name: str = "Test Model"):
    return {
        "id": model_id,
        "base_model_id": None,
        "name": name,
        "meta": {"description": "test", "profile_image_url": "/static/favicon.png"},
        "params": {},
        "access_control": None,
        "is_active": True,
    }


@pytest.mark.tier0
def test_list_models(authenticated_user):
    resp = authenticated_user.get("/api/v1/models/")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.tier0
def test_create_model(authenticated_admin):
    resp = authenticated_admin.post(
        "/api/v1/models/create",
        json=_model_payload("test-model-1", "Test Model 1"),
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == "test-model-1"


@pytest.mark.tier0
def test_create_duplicate_model_rejected(authenticated_admin):
    authenticated_admin.post(
        "/api/v1/models/create",
        json=_model_payload("dup-model"),
    )
    resp = authenticated_admin.post(
        "/api/v1/models/create",
        json=_model_payload("dup-model"),
    )
    # 401 is what the router returns for MODEL_ID_TAKEN
    assert resp.status_code in (400, 401, 409)


@pytest.mark.tier0
def test_get_model_by_id(authenticated_admin):
    authenticated_admin.post(
        "/api/v1/models/create",
        json=_model_payload("get-me", "GetMe"),
    )
    resp = authenticated_admin.get("/api/v1/models/model?id=get-me")
    assert resp.status_code == 200
    assert resp.json()["id"] == "get-me"


@pytest.mark.tier0
def test_update_model(authenticated_admin):
    authenticated_admin.post(
        "/api/v1/models/create",
        json=_model_payload("upd-me", "Old Name"),
    )
    resp = authenticated_admin.post(
        "/api/v1/models/model/update?id=upd-me",
        json=_model_payload("upd-me", "New Name"),
    )
    assert resp.status_code == 200
    refetch = authenticated_admin.get("/api/v1/models/model?id=upd-me").json()
    assert refetch["name"] == "New Name"


@pytest.mark.tier0
def test_delete_model(authenticated_admin):
    authenticated_admin.post(
        "/api/v1/models/create",
        json=_model_payload("del-me"),
    )
    resp = authenticated_admin.delete("/api/v1/models/model/delete?id=del-me")
    assert resp.status_code == 200


@pytest.mark.tier0
def test_user_cannot_create_model(user_without_workspace_permissions):
    """User explicitly without workspace.models permission is denied."""
    resp = user_without_workspace_permissions.post(
        "/api/v1/models/create",
        json=_model_payload("user-attempt"),
    )
    assert resp.status_code == 401
