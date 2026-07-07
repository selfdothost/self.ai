"""
T-304: Folders router CRUD, reparent, recursive ops, chat assignment.
"""

import pytest


@pytest.mark.tier0
def test_list_folders_empty(authenticated_user):
    resp = authenticated_user.get("/api/v1/folders/")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.tier0
def test_create_folder(authenticated_user):
    resp = authenticated_user.post("/api/v1/folders/", json={"name": "My Folder"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "My Folder"


@pytest.mark.tier0
def test_create_duplicate_folder_rejected(authenticated_user):
    authenticated_user.post("/api/v1/folders/", json={"name": "Dup"})
    resp = authenticated_user.post("/api/v1/folders/", json={"name": "Dup"})
    assert resp.status_code == 400


@pytest.mark.tier0
def test_get_folder_by_id(authenticated_user):
    created = authenticated_user.post(
        "/api/v1/folders/", json={"name": "Specific"}
    ).json()
    resp = authenticated_user.get(f"/api/v1/folders/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Specific"


@pytest.mark.tier0
def test_get_folder_not_found(authenticated_user):
    resp = authenticated_user.get("/api/v1/folders/nonexistent-id")
    assert resp.status_code == 404


@pytest.mark.tier0
def test_update_folder_name(authenticated_user):
    created = authenticated_user.post(
        "/api/v1/folders/", json={"name": "Old"}
    ).json()
    resp = authenticated_user.post(
        f"/api/v1/folders/{created['id']}/update", json={"name": "New"}
    )
    assert resp.status_code == 200
    refetch = authenticated_user.get(f"/api/v1/folders/{created['id']}").json()
    assert refetch["name"] == "New"


@pytest.mark.tier0
def test_delete_folder(authenticated_user):
    created = authenticated_user.post(
        "/api/v1/folders/", json={"name": "ToDelete"}
    ).json()
    resp = authenticated_user.delete(f"/api/v1/folders/{created['id']}")
    assert resp.status_code == 200
    # After delete, get returns 404
    refetch = authenticated_user.get(f"/api/v1/folders/{created['id']}")
    assert refetch.status_code == 404


@pytest.mark.tier0
def test_reparent_folder(authenticated_user):
    """Move a child folder under a different parent."""
    parent1 = authenticated_user.post(
        "/api/v1/folders/", json={"name": "Parent1"}
    ).json()
    parent2 = authenticated_user.post(
        "/api/v1/folders/", json={"name": "Parent2"}
    ).json()
    child = authenticated_user.post(
        "/api/v1/folders/", json={"name": "Child"}
    ).json()
    resp = authenticated_user.post(
        f"/api/v1/folders/{child['id']}/update/parent",
        json={"parent_id": parent1["id"]},
    )
    assert resp.status_code == 200
    # Move again
    resp = authenticated_user.post(
        f"/api/v1/folders/{child['id']}/update/parent",
        json={"parent_id": parent2["id"]},
    )
    assert resp.status_code == 200


@pytest.mark.tier0
def test_folder_cross_user_isolation(authenticated_user, db_session):
    """User A cannot access user B's folder."""
    from tests.factories import UserFactory
    from selfai_ui.models.folders import Folder
    import uuid, time

    user_b = UserFactory.create(db_session)
    folder_b = Folder(
        id=str(uuid.uuid4()),
        parent_id=None,
        user_id=user_b.id,
        name="B's Folder",
        items=None,
        meta=None,
        is_expanded=False,
        created_at=int(time.time()),
        updated_at=int(time.time()),
    )
    db_session.add(folder_b)
    db_session.commit()

    resp = authenticated_user.get(f"/api/v1/folders/{folder_b.id}")
    # folders.py:97 uses get_folder_by_id_and_user_id which scopes by
    # user; not-found path raises 404 with NOT_FOUND.
    assert resp.status_code == 404
