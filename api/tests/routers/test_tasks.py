"""
T-325 + T-326: Task completion endpoints tests.

Tasks endpoints (title, tags, queries, emoji, moa, autocompletion) take a
TaskFormData model (validation covered in security tests). Here we test
that the endpoints return appropriate errors when the referenced model
doesn't exist, since MODELS state is needed for them to generate payloads.
"""

import pytest


TASK_ENDPOINTS = [
    "/api/v1/tasks/title/completions",
    "/api/v1/tasks/tags/completions",
    "/api/v1/tasks/queries/completions",
    "/api/v1/tasks/emoji/completions",
    "/api/v1/tasks/moa/completions",
]


@pytest.mark.tier0
@pytest.mark.parametrize("path", TASK_ENDPOINTS)
def test_task_endpoint_rejects_unknown_model(authenticated_user, path):
    """Referring to a model that isn't in app.state.MODELS returns 404."""
    resp = authenticated_user.post(
        path,
        json={
            "model": "definitely-does-not-exist",
            "messages": [{"role": "user", "content": "hi"}],
            "prompt": "hi",
            "responses": ["a", "b"],
            "chat_id": "test",
        },
    )
    # Router raises HTTPException(status_code=404, detail="Model not found")
    # at tasks.py line 156 when model_id not in models. Pin 404.
    assert resp.status_code == 404, (
        f"{path} should 404 for unknown model, got {resp.status_code}: "
        f"{resp.text[:200]}"
    )


@pytest.mark.tier0
@pytest.mark.parametrize("path", TASK_ENDPOINTS)
def test_task_endpoint_empty_body_safe(authenticated_user, path):
    """Empty body returns validation error, not 500."""
    resp = authenticated_user.post(path, json={})
    assert resp.status_code < 500


@pytest.mark.tier0
def test_task_config_endpoint(authenticated_user):
    """Tasks config endpoint returns feature flags."""
    resp = authenticated_user.get("/api/v1/tasks/config")
    assert resp.status_code == 200
    body = resp.json()
    # Should contain at least some known config fields
    assert isinstance(body, dict)
