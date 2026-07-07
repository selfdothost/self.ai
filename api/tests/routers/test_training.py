"""
T-320 + T-321 + T-322: Training courses and jobs router tests.

Training requires admin role or workspace.training permission.
Sync tests mock the Llamolotl upstream service.
"""

import pytest


@pytest.mark.tier0
def test_list_courses_empty(authenticated_admin):
    resp = authenticated_admin.get("/api/v1/training/courses")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.tier0
def test_create_course(authenticated_admin):
    resp = authenticated_admin.post(
        "/api/v1/training/courses/create",
        json={"name": "Test Course", "description": "test desc"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Test Course"


@pytest.mark.tier0
def test_user_cannot_create_course_without_permission(
    user_without_workspace_permissions,
):
    """User explicitly without workspace.training permission is denied."""
    resp = user_without_workspace_permissions.post(
        "/api/v1/training/courses/create",
        json={"name": "User Course", "description": "test"},
    )
    assert resp.status_code == 401


@pytest.mark.tier0
def test_get_course_by_id(authenticated_admin):
    created = authenticated_admin.post(
        "/api/v1/training/courses/create",
        json={"name": "Fetchable", "description": ""},
    ).json()
    resp = authenticated_admin.get(f"/api/v1/training/courses/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Fetchable"


@pytest.mark.tier0
def test_update_course(authenticated_admin):
    created = authenticated_admin.post(
        "/api/v1/training/courses/create",
        json={"name": "Old", "description": "old"},
    ).json()
    resp = authenticated_admin.post(
        f"/api/v1/training/courses/{created['id']}/update",
        json={"name": "New", "description": "new"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "New"


@pytest.mark.tier0
def test_delete_course(authenticated_admin):
    created = authenticated_admin.post(
        "/api/v1/training/courses/create",
        json={"name": "Del", "description": ""},
    ).json()
    resp = authenticated_admin.delete(
        f"/api/v1/training/courses/{created['id']}/delete"
    )
    assert resp.status_code == 200


@pytest.mark.tier0
def test_list_jobs_empty(authenticated_admin):
    resp = authenticated_admin.get("/api/v1/training/jobs")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.tier0
def test_create_job_requires_valid_course(authenticated_admin):
    """Creating a job with a nonexistent course_id returns error."""
    resp = authenticated_admin.post(
        "/api/v1/training/jobs/create",
        json={"course_id": "nonexistent-course-id", "model_id": "test-model"},
    )
    # Should fail — invalid course
    assert resp.status_code in (400, 404)


@pytest.mark.tier0
def test_delete_nonexistent_job(authenticated_admin):
    resp = authenticated_admin.delete(
        "/api/v1/training/jobs/nonexistent/delete"
    )
    assert resp.status_code in (400, 404)


@pytest.mark.tier0
def test_sync_nonexistent_job(authenticated_admin):
    resp = authenticated_admin.post(
        "/api/v1/training/jobs/nonexistent/sync"
    )
    assert resp.status_code in (400, 404)
