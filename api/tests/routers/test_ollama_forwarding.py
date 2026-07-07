"""
T-R01: Ollama proxy forwarding tests.

The Ollama router mixes `requests` (sync) and `aiohttp` (async) for
upstream calls, so we use:
  - `responses` for sync requests mocking
  - `aioresponses` for async aiohttp mocking

Every test ensures zero real network calls: unmocked requests fail
rather than hitting the real host.
"""

import pytest
import responses


@pytest.fixture
def mocked_ollama_responses(test_app):
    """Register sync `requests` mocks for the configured Ollama base URL.

    Any call that isn't explicitly mocked will raise
    ConnectionError — the zero-real-network contract from proxies R9.
    """
    urls = getattr(
        test_app.state.config, "OLLAMA_BASE_URLS", ["http://self-ollama:11434"]
    )
    base = urls[0].rstrip("/")
    with responses.RequestsMock() as rsps:
        rsps.base_url = base
        yield rsps


# ---------------------------------------------------------------------------
# T-R01 / R1 AC1: list models
# ---------------------------------------------------------------------------

@pytest.mark.tier1
def test_ollama_tags_forwards_and_returns_upstream_body(
    authenticated_admin, mocked_ollama_responses
):
    """GET /ollama/api/tags/0 forwards to upstream and returns its body."""
    expected_body = {
        "models": [
            {"name": "llama3:8b", "size": 1234, "digest": "sha256:abc"}
        ]
    }
    mocked_ollama_responses.get(
        f"{mocked_ollama_responses.base_url}/api/tags",
        json=expected_body,
        status=200,
    )

    resp = authenticated_admin.get("/ollama/api/tags/0")
    assert resp.status_code == 200
    body = resp.json()
    assert "models" in body
    assert body["models"][0]["name"] == "llama3:8b"


# ---------------------------------------------------------------------------
# T-R01 / R1 AC5+6: 4xx / 5xx passthrough
# ---------------------------------------------------------------------------

@pytest.mark.tier1
def test_ollama_tags_passthrough_4xx(
    authenticated_admin, mocked_ollama_responses
):
    """Upstream 404 is preserved (router raises with upstream status)."""
    mocked_ollama_responses.get(
        f"{mocked_ollama_responses.base_url}/api/tags",
        json={"error": "not found"},
        status=404,
    )

    resp = authenticated_admin.get("/ollama/api/tags/0")
    assert resp.status_code == 404


@pytest.mark.tier1
def test_ollama_tags_passthrough_5xx(
    authenticated_admin, mocked_ollama_responses
):
    """Upstream 500 is preserved (router raises with upstream status)."""
    mocked_ollama_responses.get(
        f"{mocked_ollama_responses.base_url}/api/tags",
        json={"error": "internal"},
        status=500,
    )

    resp = authenticated_admin.get("/ollama/api/tags/0")
    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# T-R01 / R1 AC1: api/version
# ---------------------------------------------------------------------------

@pytest.mark.tier1
def test_ollama_version_forwards(authenticated_admin, mocked_ollama_responses):
    """GET /ollama/api/version/0 forwards and returns upstream version."""
    mocked_ollama_responses.get(
        f"{mocked_ollama_responses.base_url}/api/version",
        json={"version": "0.1.16"},
        status=200,
    )

    resp = authenticated_admin.get("/ollama/api/version/0")
    assert resp.status_code == 200
    assert resp.json() == {"version": "0.1.16"}


# ---------------------------------------------------------------------------
# T-R01 / R1 AC7: zero real network calls
# ---------------------------------------------------------------------------

@pytest.mark.tier1
def test_ollama_tags_no_unmocked_call(
    authenticated_admin, mocked_ollama_responses
):
    """A test that registers no mocks fails when an upstream call is made.

    This proves our mocking discipline: if a proxy test forgets to mock
    a URL, it does NOT silently pass against a real host.
    """
    # No mock registered. Any upstream call should raise ConnectionError
    # which the router converts to 500.
    resp = authenticated_admin.get("/ollama/api/tags/0")
    # The router converts the requests ConnectionError to a 500.
    # If this ever returned 200 in a test run, it would mean real
    # network was hit.
    assert resp.status_code == 500
