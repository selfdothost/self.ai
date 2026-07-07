"""
T-310: Prompts router CRUD tests.

Prompts require admin role or workspace.prompts permission.
"""

import pytest


@pytest.mark.tier0
def test_list_prompts_empty(authenticated_admin):
    resp = authenticated_admin.get("/api/v1/prompts/")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.tier0
def test_create_prompt(authenticated_admin):
    resp = authenticated_admin.post(
        "/api/v1/prompts/create",
        json={
            "command": "/greet",
            "title": "Greeting",
            "content": "Say hello",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["command"] == "/greet"


@pytest.mark.tier0
def test_create_duplicate_command_rejected(authenticated_admin):
    authenticated_admin.post(
        "/api/v1/prompts/create",
        json={"command": "/dupe", "title": "t1", "content": "c1"},
    )
    resp = authenticated_admin.post(
        "/api/v1/prompts/create",
        json={"command": "/dupe", "title": "t2", "content": "c2"},
    )
    assert resp.status_code == 400


@pytest.mark.tier0
def test_get_prompt_by_command(authenticated_admin):
    authenticated_admin.post(
        "/api/v1/prompts/create",
        json={"command": "/fetch", "title": "Fetch", "content": "do"},
    )
    resp = authenticated_admin.get("/api/v1/prompts/command/fetch")
    assert resp.status_code == 200
    assert resp.json()["command"] == "/fetch"


@pytest.mark.tier0
def test_update_prompt(authenticated_admin):
    authenticated_admin.post(
        "/api/v1/prompts/create",
        json={"command": "/upd", "title": "Old", "content": "old content"},
    )
    resp = authenticated_admin.post(
        "/api/v1/prompts/command/upd/update",
        json={"command": "/upd", "title": "New", "content": "new content"},
    )
    assert resp.status_code == 200
    refetch = authenticated_admin.get("/api/v1/prompts/command/upd").json()
    assert refetch["title"] == "New"


@pytest.mark.tier0
def test_delete_prompt(authenticated_admin):
    authenticated_admin.post(
        "/api/v1/prompts/create",
        json={"command": "/del", "title": "Del", "content": "delete"},
    )
    resp = authenticated_admin.delete("/api/v1/prompts/command/del/delete")
    assert resp.status_code == 200


@pytest.mark.tier0
def test_get_nonexistent_prompt(authenticated_admin):
    resp = authenticated_admin.get("/api/v1/prompts/command/nonexistent")
    # 200 with null, 404, or 401
    if resp.status_code == 200:
        assert resp.json() is None
    else:
        assert resp.status_code in (401, 404)


@pytest.mark.tier0
def test_user_without_permission_cannot_create(user_without_workspace_permissions):
    """User explicitly without workspace.prompts permission is denied."""
    resp = user_without_workspace_permissions.post(
        "/api/v1/prompts/create",
        json={"command": "/denied", "title": "t", "content": "c"},
    )
    assert resp.status_code == 401
