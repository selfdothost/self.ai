"""
T-229 + T-230: Input validation tests on dict-typed endpoints.

Covers 10+ endpoints that accept raw `dict` bodies without Pydantic schemas.
Validates that missing/extra/wrong-type fields don't crash the server.
"""

import pytest


# Endpoints identified in research brief that accept raw dict bodies
# Format: (method, path, minimal_body) — all require auth
DICT_ENDPOINTS = [
    ("POST", "/api/chat/completions", {}),
    ("POST", "/api/completions", {}),
    ("POST", "/api/chat/completed", {}),
    ("POST", "/api/chat/actions/test-action-id", {}),
    ("POST", "/api/v1/users/user/info/update", {}),
    # Lower-risk but still dict-typed
    ("POST", "/api/v1/tasks/title/completions", {}),
    ("POST", "/api/v1/tasks/tags/completions", {}),
    ("POST", "/api/v1/tasks/queries/completions", {}),
    ("POST", "/api/v1/tasks/emoji/completions", {}),
    ("POST", "/api/v1/tasks/moa/completions", {}),
]


# ---------------------------------------------------------------------------
# T-229 + T-230: Dict endpoint validation
# ---------------------------------------------------------------------------

@pytest.mark.tier0
@pytest.mark.security
@pytest.mark.parametrize("method,path,body", DICT_ENDPOINTS)
def test_empty_body_no_server_error(authenticated_user, method, path, body):
    """Empty request body must not cause 500."""
    resp = authenticated_user.request(method, path, json=body)
    assert resp.status_code < 500, (
        f"{method} {path} with empty body returned {resp.status_code}: "
        f"{resp.text[:300]}"
    )


@pytest.mark.tier0
@pytest.mark.security
@pytest.mark.parametrize("method,path,_", DICT_ENDPOINTS)
def test_extra_fields_no_server_error(authenticated_user, method, path, _):
    """Unexpected extra fields must not cause 500."""
    body = {
        "garbage_field_1": "random",
        "garbage_field_2": 12345,
        "nested": {"unknown": True},
    }
    resp = authenticated_user.request(method, path, json=body)
    assert resp.status_code < 500, (
        f"{method} {path} with extra fields returned {resp.status_code}"
    )


@pytest.mark.tier0
@pytest.mark.security
@pytest.mark.parametrize("method,path,_", DICT_ENDPOINTS)
def test_wrong_type_no_server_error(authenticated_user, method, path, _):
    """Wrong-type values (string where int expected) must not cause 500."""
    body = {
        "messages": "not-a-list",
        "model": ["wrong", "type"],
        "temperature": "not-a-float",
        "max_tokens": "not-an-int",
    }
    resp = authenticated_user.request(method, path, json=body)
    assert resp.status_code < 500, (
        f"{method} {path} with wrong types returned {resp.status_code}"
    )


@pytest.mark.tier0
@pytest.mark.security
@pytest.mark.parametrize("method,path,_", DICT_ENDPOINTS)
def test_deeply_nested_no_stack_overflow(authenticated_user, method, path, _):
    """Deeply nested objects do not cause stack overflow or timeout."""
    # Build a 100-level deep nested dict
    body = {"x": None}
    current = body
    for _ in range(100):
        current["x"] = {"x": None}
        current = current["x"]

    resp = authenticated_user.request(method, path, json=body, timeout=10)
    # Should not crash or hang
    assert resp.status_code < 500, (
        f"{method} {path} with deep nesting returned {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Coverage confirmation
# ---------------------------------------------------------------------------

@pytest.mark.tier0
@pytest.mark.security
def test_coverage_at_least_ten_dict_endpoints():
    """The test suite covers at least 10 dict-typed endpoints."""
    assert len(DICT_ENDPOINTS) >= 10, (
        f"Only {len(DICT_ENDPOINTS)} endpoints covered — research brief "
        f"identified 19+ dict endpoints, test at least 10."
    )
