"""
T-312: Functions router admin-only CRUD tests.

Functions are admin-only to create/delete. Regular users can list
(for their active filters/pipelines).
"""

import pytest


VALID_FUNCTION_CONTENT = '''"""
title: TestFilter
description: Minimal valid function for testing
"""


class Filter:
    def __init__(self):
        pass

    def inlet(self, body: dict) -> dict:
        return body

    def outlet(self, body: dict) -> dict:
        return body
'''


@pytest.mark.tier0
def test_list_functions_empty(authenticated_admin):
    resp = authenticated_admin.get("/api/v1/functions/")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.tier0
def test_any_user_can_list_functions(authenticated_user):
    """List functions is open to any verified user (for filter consumption)."""
    resp = authenticated_user.get("/api/v1/functions/")
    assert resp.status_code == 200


@pytest.mark.tier0
def test_user_cannot_create_function(authenticated_user):
    resp = authenticated_user.post(
        "/api/v1/functions/create",
        json={
            "id": "user_fn",
            "name": "User Fn",
            "type": "filter",
            "content": VALID_FUNCTION_CONTENT,
            "meta": {"description": ""},
        },
    )
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_admin_create_function(authenticated_admin):
    """Admin creates a filter function with valid Python content."""
    resp = authenticated_admin.post(
        "/api/v1/functions/create",
        json={
            "id": "test_fn",
            "name": "Test Function",
            "type": "filter",
            "content": VALID_FUNCTION_CONTENT,
            "meta": {"description": "Test"},
        },
    )
    assert resp.status_code == 200, (
        f"Function create returned {resp.status_code}: {resp.text[:200]}"
    )


@pytest.mark.tier0
def test_get_function_not_found(authenticated_admin):
    resp = authenticated_admin.get("/api/v1/functions/id/nonexistent_fn")
    if resp.status_code == 200:
        assert resp.json() is None
    else:
        assert resp.status_code in (401, 404)
