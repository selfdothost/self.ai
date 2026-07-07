"""
T-307 + T-308: Knowledge bases router CRUD and file membership tests.

Knowledge creation requires admin role or workspace.knowledge permission.
We use authenticated_admin for create/delete tests.
"""

import pytest


@pytest.mark.tier0
def test_list_knowledge_empty(authenticated_admin):
    resp = authenticated_admin.get("/api/v1/knowledge/")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.tier0
def test_create_knowledge_base(authenticated_admin):
    resp = authenticated_admin.post(
        "/api/v1/knowledge/create",
        json={
            "name": "Test KB",
            "description": "A knowledge base for tests",
            "access_control": None,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Test KB"


@pytest.mark.tier0
def test_get_knowledge_by_id(authenticated_admin):
    created = authenticated_admin.post(
        "/api/v1/knowledge/create",
        json={"name": "Specific KB", "description": "test"},
    ).json()
    kb_id = created["id"]

    resp = authenticated_admin.get(f"/api/v1/knowledge/{kb_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == kb_id
    assert body["name"] == "Specific KB"
    assert "files" in body  # KnowledgeFilesResponse includes files list


@pytest.mark.tier0
def test_update_knowledge(authenticated_admin):
    created = authenticated_admin.post(
        "/api/v1/knowledge/create",
        json={"name": "Old Name", "description": "old desc"},
    ).json()
    kb_id = created["id"]

    resp = authenticated_admin.post(
        f"/api/v1/knowledge/{kb_id}/update",
        json={"name": "New Name", "description": "new desc"},
    )
    assert resp.status_code == 200
    refetch = authenticated_admin.get(f"/api/v1/knowledge/{kb_id}").json()
    assert refetch["name"] == "New Name"


@pytest.mark.tier0
def test_delete_knowledge(authenticated_admin):
    created = authenticated_admin.post(
        "/api/v1/knowledge/create",
        json={"name": "Delete KB", "description": ""},
    ).json()
    kb_id = created["id"]

    resp = authenticated_admin.delete(f"/api/v1/knowledge/{kb_id}/delete")
    assert resp.status_code == 200
    # Refetch should return not-found
    refetch = authenticated_admin.get(f"/api/v1/knowledge/{kb_id}")
    # 200 with null, or 4xx
    if refetch.status_code == 200:
        assert refetch.json() is None


@pytest.mark.tier0
def test_knowledge_get_not_found(authenticated_admin):
    resp = authenticated_admin.get("/api/v1/knowledge/bogus-id-12345")
    # 200 null or 404
    if resp.status_code == 200:
        assert resp.json() is None
    else:
        assert resp.status_code in (401, 404)


@pytest.mark.tier0
def test_knowledge_creation_denied_for_user_without_permission(
    user_without_workspace_permissions,
):
    """Regular user without workspace.knowledge permission is denied."""
    resp = user_without_workspace_permissions.post(
        "/api/v1/knowledge/create",
        json={"name": "Denied KB", "description": ""},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# T-R18: File membership (add/remove/reset) error paths
# ---------------------------------------------------------------------------

@pytest.mark.tier0
def test_add_file_to_nonexistent_kb_rejected(authenticated_admin):
    """Adding a file to a KB that doesn't exist returns 400 NOT_FOUND."""
    resp = authenticated_admin.post(
        "/api/v1/knowledge/nonexistent-kb-id/file/add",
        json={"file_id": "some-file-id"},
    )
    assert resp.status_code == 400


@pytest.mark.tier0
def test_add_nonexistent_file_to_kb_rejected(authenticated_admin):
    """Adding a nonexistent file to an existing KB returns 400 NOT_FOUND."""
    kb = authenticated_admin.post(
        "/api/v1/knowledge/create",
        json={"name": "Member KB", "description": ""},
    ).json()
    resp = authenticated_admin.post(
        f"/api/v1/knowledge/{kb['id']}/file/add",
        json={"file_id": "nonexistent-file-id"},
    )
    assert resp.status_code == 400


@pytest.mark.tier0
def test_remove_file_from_nonexistent_kb_rejected(authenticated_admin):
    """Removing a file from a nonexistent KB returns 400 NOT_FOUND."""
    resp = authenticated_admin.post(
        "/api/v1/knowledge/nonexistent-kb-id/file/remove",
        json={"file_id": "some-file-id"},
    )
    assert resp.status_code == 400


@pytest.mark.tier0
def test_reset_nonexistent_kb_rejected(authenticated_admin):
    """Resetting a nonexistent KB returns 400 NOT_FOUND."""
    resp = authenticated_admin.post(
        "/api/v1/knowledge/nonexistent-kb-id/reset"
    )
    assert resp.status_code == 400


@pytest.mark.tier0
def test_reset_existing_kb_clears_membership(authenticated_admin):
    """Resetting an existing KB succeeds (empty KB stays empty)."""
    kb = authenticated_admin.post(
        "/api/v1/knowledge/create",
        json={"name": "Reset Me", "description": ""},
    ).json()
    resp = authenticated_admin.post(f"/api/v1/knowledge/{kb['id']}/reset")
    # Router returns 200 with updated KB (no files)
    assert resp.status_code == 200


@pytest.mark.tier0
def test_user_cannot_add_file_to_other_users_kb(
    authenticated_user, db_session
):
    """User A cannot add a file to user B's knowledge base."""
    from tests.factories import UserFactory, KnowledgeFactory
    user_b = UserFactory.create(db_session)
    kb_b = KnowledgeFactory.create(
        db_session, user_id=user_b.id, name="B's KB"
    )
    resp = authenticated_user.post(
        f"/api/v1/knowledge/{kb_b.id}/file/add",
        json={"file_id": "any-file"},
    )
    # Router raises 400 ACCESS_PROHIBITED when caller is not owner/admin
    assert resp.status_code == 400
