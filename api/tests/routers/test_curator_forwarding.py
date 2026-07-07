"""
T-R03 (part 3): Curator proxy forwarding.

Curator router uses aiohttp for verify + job submission upstream calls.
"""

import pytest
from aioresponses import aioresponses
from tests.mocks.external_services import aioresponses_strict


@pytest.mark.tier1
def test_curator_verify_success(authenticated_admin):
    """POST /curator/verify returns the upstream /health body on 200."""
    target = "http://self-curator:8000"
    with aioresponses_strict() as m:
        m.get(
            f"{target}/health",
            status=200,
            payload={"status": "healthy", "version": "1.0"},
        )
        resp = authenticated_admin.post(
            "/curator/verify", json={"url": target}
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


@pytest.mark.tier1
def test_curator_verify_upstream_error_becomes_500(authenticated_admin):
    target = "http://self-curator:8000"
    with aioresponses_strict() as m:
        m.get(
            f"{target}/health",
            status=503,
            payload={"error": "service down"},
        )
        resp = authenticated_admin.post(
            "/curator/verify", json={"url": target}
        )
    assert resp.status_code == 500


@pytest.mark.tier1
def test_curator_verify_strips_trailing_slash(authenticated_admin):
    """Router strips trailing slash before probing /health.

    Also asserts the captured request went to {url}/health (no double
    slashes), verifying the forwarding path is correct.
    """
    from aioresponses import CallbackResult
    target = "http://self-curator:8000"
    captured_urls = []

    def capture(url, **kwargs):
        captured_urls.append(str(url))
        return CallbackResult(status=200, payload={"ok": True})

    with aioresponses_strict() as m:
        m.get(f"{target}/health", callback=capture)
        resp = authenticated_admin.post(
            "/curator/verify", json={"url": target + "/"}
        )
    assert resp.status_code == 200
    assert len(captured_urls) == 1
    # URL should NOT contain double slash after host
    assert "//health" not in captured_urls[0].replace("://", ":")
    assert captured_urls[0].endswith("/health")


@pytest.mark.tier1
def test_curator_config_update_persists(authenticated_admin):
    """Config update round-trip using current config shape succeeds."""
    current = authenticated_admin.get("/curator/config").json()
    resp = authenticated_admin.post("/curator/config/update", json=current)
    assert resp.status_code == 200, (
        f"Config round-trip returned {resp.status_code}: {resp.text[:200]}"
    )
