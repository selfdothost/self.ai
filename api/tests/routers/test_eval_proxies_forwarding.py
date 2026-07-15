"""
T-R03 (part 1): language-eval + code-eval verify endpoint forwarding.

Both verify endpoints use aiohttp.ClientSession to GET {url}/health
on the provided URL. Tests use aioresponses to mock the upstream.
"""

import pytest

from tests.mocks.external_services import aioresponses_strict


@pytest.mark.tier1
def test_language_eval_verify_success(authenticated_admin):
    """A 200 from upstream /health returns the JSON body to the caller."""
    target = "http://self-language-eval:5000"
    with aioresponses_strict() as m:
        m.get(f"{target}/health", status=200, payload={"status": "healthy"})
        resp = authenticated_admin.post("/language-eval/verify", json={"url": target})
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}


@pytest.mark.tier1
def test_language_eval_verify_upstream_non_200_becomes_500(authenticated_admin):
    """Upstream 503 is caught and converted to 500 per router code."""
    target = "http://self-language-eval:5000"
    with aioresponses_strict() as m:
        m.get(f"{target}/health", status=503, payload={"error": "down"})
        resp = authenticated_admin.post("/language-eval/verify", json={"url": target})
    assert resp.status_code == 500


@pytest.mark.tier1
def test_code_eval_verify_success(authenticated_admin):
    target = "http://self-code-eval:5001"
    with aioresponses_strict() as m:
        m.get(f"{target}/health", status=200, payload={"status": "ok"})
        resp = authenticated_admin.post("/code-eval/verify", json={"url": target})
    assert resp.status_code == 200


@pytest.mark.tier1
def test_code_eval_verify_upstream_non_200_becomes_500(authenticated_admin):
    target = "http://self-code-eval:5001"
    with aioresponses_strict() as m:
        m.get(f"{target}/health", status=404, payload={"error": "missing"})
        resp = authenticated_admin.post("/code-eval/verify", json={"url": target})
    assert resp.status_code == 500


@pytest.mark.tier1
def test_language_eval_config_update_persists(authenticated_admin):
    """PUT /language-eval/config/update persists ENABLE_LANGUAGE_EVAL_API and base URLs."""
    resp = authenticated_admin.post(
        "/language-eval/config/update",
        json={
            "ENABLE_LANGUAGE_EVAL_API": True,
            "LANGUAGE_EVAL_BASE_URLS": ["http://self-language-eval:5000"],
        },
    )
    assert resp.status_code == 200
    # Re-fetch to verify persistence
    refetch = authenticated_admin.get("/language-eval/config").json()
    assert refetch["ENABLE_LANGUAGE_EVAL_API"] is True
    assert "http://self-language-eval:5000" in refetch["LANGUAGE_EVAL_BASE_URLS"]


@pytest.mark.tier1
def test_code_eval_config_update_persists(authenticated_admin):
    resp = authenticated_admin.post(
        "/code-eval/config/update",
        json={
            "ENABLE_CODE_EVAL_API": True,
            "CODE_EVAL_BASE_URLS": ["http://self-code-eval:5001"],
        },
    )
    assert resp.status_code == 200
    refetch = authenticated_admin.get("/code-eval/config").json()
    assert refetch["ENABLE_CODE_EVAL_API"] is True
    assert "http://self-code-eval:5001" in refetch["CODE_EVAL_BASE_URLS"]
