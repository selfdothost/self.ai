"""
T-323 + T-324: Eval jobs router tests.

Eval job create/approve/reject/cancel + SSE stream tests.
"""

import pytest


@pytest.mark.tier0
def test_list_eval_jobs_empty(authenticated_admin):
    resp = authenticated_admin.get("/api/v1/evaluations/jobs")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.tier0
def test_create_eval_job_code_eval(authenticated_admin, seeded_benchmarks):
    resp = authenticated_admin.post(
        "/api/v1/evaluations/jobs/create",
        json={
            "eval_type": "code-eval",
            "benchmark": "humaneval",
            "model_id": "test-model",
        },
    )
    assert resp.status_code == 200, f"Eval create returned {resp.status_code}: {resp.text[:200]}"
    body = resp.json()
    assert body["eval_type"] == "code-eval"
    assert body["benchmark"] == "humaneval"


@pytest.mark.tier0
def test_create_eval_job_language_eval(authenticated_admin, seeded_benchmarks):
    resp = authenticated_admin.post(
        "/api/v1/evaluations/jobs/create",
        json={
            "eval_type": "language-eval",
            "benchmark": "mmlu",
            "model_id": "test-model",
        },
    )
    assert resp.status_code == 200, f"Eval create returned {resp.status_code}: {resp.text[:200]}"
    assert resp.json()["eval_type"] == "language-eval"


@pytest.mark.tier0
def test_user_without_permission_cannot_create_eval_job(
    user_without_workspace_permissions,
):
    """User explicitly without workspace.evaluations permission is denied."""
    resp = user_without_workspace_permissions.post(
        "/api/v1/evaluations/jobs/create",
        json={
            "eval_type": "code-eval",
            "benchmark": "humaneval",
            "model_id": "test-model",
        },
    )
    assert resp.status_code == 401


@pytest.mark.tier0
def test_cancel_nonexistent_eval_job(authenticated_admin):
    resp = authenticated_admin.post("/api/v1/evaluations/jobs/nonexistent/cancel")
    assert resp.status_code in (400, 404)


@pytest.mark.tier0
def test_delete_nonexistent_eval_job(authenticated_admin):
    resp = authenticated_admin.delete("/api/v1/evaluations/jobs/nonexistent/delete")
    assert resp.status_code in (400, 404)


@pytest.mark.tier0
def test_get_eval_config(authenticated_admin):
    resp = authenticated_admin.get("/api/v1/evaluations/config")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Feedbacks sub-resource
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_get_user_feedbacks(authenticated_user):
    resp = authenticated_user.get("/api/v1/evaluations/feedbacks/user")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.tier0
def test_get_all_feedbacks_admin(authenticated_admin):
    resp = authenticated_admin.get("/api/v1/evaluations/feedbacks/all")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.tier0
def test_create_feedback(authenticated_user):
    resp = authenticated_user.post(
        "/api/v1/evaluations/feedback",
        json={
            "type": "rating",
            "data": {"rating": 1, "reason": "test"},
            "meta": {},
        },
    )
    assert resp.status_code == 200, f"Feedback create returned {resp.status_code}: {resp.text[:200]}"
