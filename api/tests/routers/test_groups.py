"""T-314: Groups router admin-only CRUD tests."""

import pytest


@pytest.mark.tier0
def test_list_groups_empty(authenticated_admin):
    resp = authenticated_admin.get("/api/v1/groups/")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.tier0
def test_create_group(authenticated_admin):
    resp = authenticated_admin.post(
        "/api/v1/groups/create",
        json={"name": "devs", "description": "developers"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "devs"


@pytest.mark.tier0
def test_get_group_by_id(authenticated_admin):
    created = authenticated_admin.post(
        "/api/v1/groups/create",
        json={"name": "g-fetch", "description": ""},
    ).json()
    resp = authenticated_admin.get(f"/api/v1/groups/id/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]


@pytest.mark.tier0
def test_update_group(authenticated_admin):
    created = authenticated_admin.post(
        "/api/v1/groups/create",
        json={"name": "g-upd", "description": "old"},
    ).json()
    resp = authenticated_admin.post(
        f"/api/v1/groups/id/{created['id']}/update",
        json={
            "name": "g-upd-new",
            "description": "new",
            "permissions": None,
            "user_ids": [],
            "admin_ids": [],
        },
    )
    assert resp.status_code == 200
    refetch = authenticated_admin.get(
        f"/api/v1/groups/id/{created['id']}"
    ).json()
    assert refetch["description"] == "new"


@pytest.mark.tier0
def test_delete_group(authenticated_admin):
    created = authenticated_admin.post(
        "/api/v1/groups/create",
        json={"name": "g-del", "description": ""},
    ).json()
    resp = authenticated_admin.delete(
        f"/api/v1/groups/id/{created['id']}/delete"
    )
    assert resp.status_code == 200


@pytest.mark.tier0
def test_user_cannot_access_groups(authenticated_user):
    resp = authenticated_user.post(
        "/api/v1/groups/create",
        json={"name": "x", "description": "y"},
    )
    assert resp.status_code in (401, 403)
