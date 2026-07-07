"""
T-311: Tools router CRUD tests.

Note: Creating a tool triggers exec() to load the tool module. We provide
valid minimal Python content. Full exec/sandbox testing is deferred
(see project_plugin_sandbox memory).
"""

import pytest


VALID_TOOL_CONTENT = '''"""
title: TestTool
description: Minimal valid tool for testing
"""


class Tools:
    def __init__(self):
        pass

    def hello(self) -> str:
        """A test function."""
        return "hello"
'''


@pytest.mark.tier0
def test_list_tools_empty(authenticated_admin):
    resp = authenticated_admin.get("/api/v1/tools/")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.tier0
def test_create_tool_minimal(authenticated_admin):
    """Create a tool with valid Python content — must succeed."""
    resp = authenticated_admin.post(
        "/api/v1/tools/create",
        json={
            "id": "test_tool",
            "name": "Test Tool",
            "content": VALID_TOOL_CONTENT,
            "meta": {"description": "Test"},
            "access_control": None,
        },
    )
    assert resp.status_code == 200, (
        f"Tool create returned {resp.status_code}: {resp.text[:200]}"
    )
    assert resp.json()["id"] == "test_tool"


@pytest.mark.tier0
def test_create_tool_invalid_id_rejected(authenticated_admin):
    """Tool ID must be a valid Python identifier."""
    resp = authenticated_admin.post(
        "/api/v1/tools/create",
        json={
            "id": "invalid-id!",  # hyphens/bangs aren't identifiers
            "name": "Bad",
            "content": VALID_TOOL_CONTENT,
            "meta": {"description": ""},
            "access_control": None,
        },
    )
    assert resp.status_code == 400


@pytest.mark.tier0
def test_get_tool_not_found(authenticated_admin):
    resp = authenticated_admin.get("/api/v1/tools/id/nonexistent_tool")
    # 200 null or 401
    if resp.status_code == 200:
        assert resp.json() is None
    else:
        assert resp.status_code in (401, 404)


@pytest.mark.tier0
def test_user_without_permission_cannot_create(user_without_workspace_permissions):
    """User explicitly without workspace permission cannot create tools."""
    resp = user_without_workspace_permissions.post(
        "/api/v1/tools/create",
        json={
            "id": "user_tool",
            "name": "User's Tool",
            "content": VALID_TOOL_CONTENT,
            "meta": {"description": ""},
            "access_control": None,
        },
    )
    # Router raises 401 UNAUTHORIZED when has_permission returns False
    assert resp.status_code == 401
