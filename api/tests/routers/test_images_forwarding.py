"""
T-R03 (part 5): Images router forwarding.

Images router uses requests for AUTOMATIC1111/ComfyUI upstreams.
Tests cover admin config endpoints and auth enforcement; full
generate-endpoint forwarding requires a configured image backend.
"""

import pytest


@pytest.mark.tier1
def test_images_config_update_persists(authenticated_admin):
    """Config round-trip with current config succeeds."""
    current = authenticated_admin.get("/api/v1/images/config").json()
    resp = authenticated_admin.post("/api/v1/images/config/update", json=current)
    assert resp.status_code == 200, (
        f"Config round-trip returned {resp.status_code}: {resp.text[:200]}"
    )


@pytest.mark.tier1
def test_images_url_verify_admin_only(authenticated_user):
    """/config/url/verify is admin-only (returns ok/error from upstream probe)."""
    resp = authenticated_user.get("/api/v1/images/config/url/verify")
    assert resp.status_code in (401, 403)


@pytest.mark.tier1
def test_images_generate_requires_auth(client):
    """POST /images/generations requires authentication."""
    resp = client.post("/api/v1/images/generations", json={"prompt": "cat"})
    assert resp.status_code in (401, 403, 404)
