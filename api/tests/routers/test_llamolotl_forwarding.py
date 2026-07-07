"""
T-R03 (part 2): Llamolotl proxy forwarding.

Llamolotl router uses aiohttp for all upstream calls. Tests use
aioresponses for mocking.
"""

import pytest
from aioresponses import aioresponses
from tests.mocks.external_services import aioresponses_strict


@pytest.mark.tier1
def test_llamolotl_verify_success(authenticated_admin):
    """POST /llamolotl/verify probes upstream /health and returns the body."""
    target = "http://self-llamolotl:8080"
    with aioresponses_strict() as m:
        m.get(
            f"{target}/health",
            status=200,
            payload={"status": "healthy", "version": "0.1.0"},
        )
        resp = authenticated_admin.post(
            "/llamolotl/verify", json={"url": target, "key": ""}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"


@pytest.mark.tier1
def test_llamolotl_verify_upstream_error_becomes_500(authenticated_admin):
    """Upstream non-200 is caught and converted to 500."""
    target = "http://self-llamolotl:8080"
    with aioresponses_strict() as m:
        m.get(f"{target}/health", status=503, payload={"error": "unavailable"})
        resp = authenticated_admin.post(
            "/llamolotl/verify", json={"url": target, "key": ""}
        )
    assert resp.status_code == 500


@pytest.mark.tier1
def test_llamolotl_verify_with_bearer_key(authenticated_admin):
    """When a key is provided, the Authorization: Bearer header is forwarded
    to the upstream /health endpoint.

    Uses aioresponses `callback=` to capture the request and assert on
    the exact forwarded Authorization header.
    """
    target = "http://self-llamolotl-auth:8080"
    captured_headers = {}

    def capture_callback(url, **kwargs):
        from aioresponses import CallbackResult
        captured_headers.update(kwargs.get("headers") or {})
        return CallbackResult(status=200, payload={"status": "ok"})

    with aioresponses_strict() as m:
        m.get(f"{target}/health", callback=capture_callback)
        resp = authenticated_admin.post(
            "/llamolotl/verify",
            json={"url": target, "key": "secret-key-123"},
        )

    assert resp.status_code == 200
    # Assert the Authorization header was forwarded with the bearer key
    auth_header = captured_headers.get("Authorization", "")
    assert auth_header == "Bearer secret-key-123", (
        f"Expected 'Bearer secret-key-123' forwarded, got: {auth_header!r}. "
        f"All captured headers: {captured_headers}"
    )


@pytest.mark.tier1
def test_llamolotl_verify_unmocked_url_fails(authenticated_admin):
    """Without a mock, connection error returns 500 with specific detail."""
    resp = authenticated_admin.post(
        "/llamolotl/verify",
        json={"url": "http://nonexistent.invalid.test:9999", "key": ""},
    )
    # Router converts aiohttp.ClientError → 500 with detail containing
    # "Self.AI UI:" prefix per llamolotl.py:273-277
    assert resp.status_code == 500, (
        f"Expected 500 connection error, got {resp.status_code}: "
        f"{resp.text[:200]}"
    )


@pytest.mark.tier1
def test_llamolotl_config_update_persists(authenticated_admin):
    """Config update round-trip with current config succeeds."""
    current = authenticated_admin.get("/llamolotl/config").json()
    resp = authenticated_admin.post("/llamolotl/config/update", json=current)
    assert resp.status_code == 200, (
        f"Config round-trip returned {resp.status_code}: {resp.text[:200]}"
    )
