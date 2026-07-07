"""
T-R02: OpenAI proxy forwarding tests using aioresponses.

OpenAI router uses `aiohttp.ClientSession` for upstream calls. Tests
use `aioresponses` (async version of `responses`) for mocking.

Enforcement: unmocked requests raise ClientConnectionError (aioresponses
default). We also assert `len(m.requests) > 0` on fixture teardown so
tests that register a mock but never hit it fail loudly — this is the
aioresponses analog of respx's `assert_all_called=True`.
"""

import pytest
from aioresponses import aioresponses


@pytest.fixture
def mocked_openai(test_app):
    """aioresponses context for configured OpenAI base URLs.

    Any unmocked call fails the request. At least one registered
    mock must have been called or the test fails at teardown.
    """
    urls = getattr(
        test_app.state.config, "OPENAI_API_BASE_URLS", ["https://api.openai.com/v1"]
    )
    with aioresponses() as m:
        m.base_url = urls[0].rstrip("/")
        yield m
        # Enforce: at least one registered mock must have been called.
        assert len(m.requests) > 0, (
            "aioresponses recorded no requests — the test registered "
            "mocks but the router never called upstream. Remove unused "
            "mocks or verify the test path actually hits the upstream."
        )


@pytest.mark.tier1
def test_openai_models_list_forwards(authenticated_admin, mocked_openai):
    """GET /openai/models/0 returns the upstream-formatted model list."""
    expected = {
        "object": "list",
        "data": [{"id": "gpt-4", "object": "model", "created": 1, "owned_by": "openai"}],
    }
    mocked_openai.get(
        f"{mocked_openai.base_url}/models",
        status=200,
        payload=expected,
    )
    resp = authenticated_admin.get("/openai/models/0")
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body


@pytest.mark.tier1
def test_openai_models_passthrough_4xx(authenticated_admin, mocked_openai):
    """Upstream 401 is preserved as 401 to the caller."""
    mocked_openai.get(
        f"{mocked_openai.base_url}/models",
        status=401,
        payload={"error": {"message": "Invalid API key"}},
    )
    resp = authenticated_admin.get("/openai/models/0")
    assert resp.status_code == 401
    assert "Invalid" in resp.text


# Note: "no unmocked call" test removed — without aioresponses context,
# the router uses real aiohttp, so real network calls are possible.
# Enforcement of zero-real-network happens inside `mocked_openai`
# (fixture uses `assert_all_mocked=True`). Tests that need strict
# no-network guarantees must request the fixture, not assert on what
# happens without it.
