"""
T-229 + T-230: Input validation tests on dict-typed endpoints.

Covers 10+ endpoints that accept raw `dict` bodies without Pydantic schemas.
Validates that missing/extra/wrong-type fields don't crash the server.
"""

from unittest.mock import MagicMock, patch

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
    assert resp.status_code < 500, f"{method} {path} with empty body returned {resp.status_code}: " f"{resp.text[:300]}"


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
    assert resp.status_code < 500, f"{method} {path} with extra fields returned {resp.status_code}"


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
    assert resp.status_code < 500, f"{method} {path} with wrong types returned {resp.status_code}"


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

    # `timeout=` on Starlette's TestClient is deprecated (in-process ASGI
    # calls don't need a network-style timeout) -- StarletteDeprecationWarning.
    resp = authenticated_user.request(method, path, json=body)
    # Should not crash or hang
    assert resp.status_code < 500, f"{method} {path} with deep nesting returned {resp.status_code}"


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


# ---------------------------------------------------------------------------
# self.ai#14: hf_path shape validation (HF proxy endpoints)
#
# Destination host is hardcoded (huggingface.co / datasets-server.huggingface.co)
# so this isn't SSRF-to-arbitrary-hosts, but an unvalidated hf_path let query
# parameters (config/split/offset/length) be overridden via unescaped &/#/?
# in the value. _validate_hf_path() closes that with a strict shape check.
# ---------------------------------------------------------------------------

MALFORMED_HF_PATHS = [
    "",
    "org/name&config=other",
    "org/name#fragment",
    "org/name?extra=1",
    "org/name/extra-segment",
    "../../etc/passwd",
]


@pytest.mark.tier0
@pytest.mark.security
@pytest.mark.parametrize("bad_hf_path", MALFORMED_HF_PATHS)
def test_hf_dataset_info_rejects_malformed_hf_path(authenticated_admin, bad_hf_path):
    resp = authenticated_admin.get("/api/v1/knowledge/hf/dataset-info", params={"hf_path": bad_hf_path})
    assert resp.status_code == 400


@pytest.mark.tier0
@pytest.mark.security
@pytest.mark.parametrize("bad_hf_path", MALFORMED_HF_PATHS)
def test_hf_dataset_rows_rejects_malformed_hf_path(authenticated_admin, bad_hf_path):
    resp = authenticated_admin.get("/api/v1/knowledge/hf/dataset-rows", params={"hf_path": bad_hf_path})
    assert resp.status_code == 400


@pytest.mark.tier0
@pytest.mark.security
@pytest.mark.parametrize("good_hf_path", ["squad", "org-name/dataset_name.v2"])
def test_validate_hf_path_accepts_legit_shapes(good_hf_path):
    from selfai_ui.routers.knowledge import _validate_hf_path

    assert _validate_hf_path(good_hf_path) == good_hf_path


# ---------------------------------------------------------------------------
# self.ai#14: SafeWebBaseLoader must re-validate redirect targets
#
# validate_url() only checks the URL the caller supplied; plain
# requests.Session.get() follows redirects transparently, so a URL that
# passes the initial check could still 302 to an internal target. This
# confirms SafeWebBaseLoader._scrape re-validates each redirect hop instead
# of following it blindly.
# ---------------------------------------------------------------------------


def _redirect_response(location):
    resp = MagicMock()
    resp.is_redirect = True
    resp.is_permanent_redirect = False
    resp.headers = {"Location": location}
    return resp


def _final_response(text="<html><body>ok</body></html>"):
    resp = MagicMock()
    resp.is_redirect = False
    resp.is_permanent_redirect = False
    resp.text = text
    resp.raise_for_status = MagicMock()
    return resp


@pytest.mark.tier0
@pytest.mark.security
def test_redirect_to_private_ip_is_rejected():
    from selfai_ui.retrieval.web.utils import SafeWebBaseLoader

    loader = SafeWebBaseLoader.__new__(SafeWebBaseLoader)
    loader.requests_kwargs = {}
    loader.session = MagicMock()
    loader.session.get.return_value = _redirect_response("http://169.254.169.254/latest/meta-data/")
    loader.default_parser = "html.parser"
    loader.raise_for_status = False
    loader.encoding = None
    loader.autoset_encoding = False

    with patch("selfai_ui.retrieval.web.utils.ENABLE_RAG_LOCAL_WEB_FETCH", False):
        with pytest.raises(ValueError):
            loader._scrape("https://example.com/redirector")


@pytest.mark.tier0
@pytest.mark.security
def test_redirect_to_public_host_is_followed():
    from selfai_ui.retrieval.web.utils import SafeWebBaseLoader

    loader = SafeWebBaseLoader.__new__(SafeWebBaseLoader)
    loader.requests_kwargs = {}
    loader.session = MagicMock()
    loader.session.get.side_effect = [
        _redirect_response("https://example.org/final"),
        _final_response(),
    ]
    loader.default_parser = "html.parser"
    loader.raise_for_status = False
    loader.encoding = None
    loader.autoset_encoding = False

    with patch("selfai_ui.retrieval.web.utils.ENABLE_RAG_LOCAL_WEB_FETCH", False), patch(
        "selfai_ui.retrieval.web.utils.resolve_hostname", return_value=(["93.184.216.34"], [])
    ):
        result = loader._scrape("https://example.com/redirector")

    assert loader.session.get.call_count == 2
    assert result.get_text() == "ok"
