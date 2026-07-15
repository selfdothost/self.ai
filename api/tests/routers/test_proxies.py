"""
External service proxy router auth-gate tests.

Covers:
- Status/health endpoints (unauthenticated where appropriate)
- Config get/set (admin-gated)
- Auth enforcement on all proxy endpoints

Forwarding and passthrough tests live in separate files per service:
- test_ollama_forwarding.py — Ollama tags/version/chat via `responses`
- test_openai_forwarding.py — OpenAI models/chat via `aioresponses`

This file intentionally does NOT mock upstreams because these tests
are testing the FastAPI auth layer only.
"""

import pytest

# ---------------------------------------------------------------------------
# T-327: Ollama proxy
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_ollama_status_unauthenticated(client):
    """GET /ollama/ is a health check that returns 200 without auth.
    The handler returns a fixed string regardless of upstream state."""
    resp = client.get("/ollama/")
    assert resp.status_code == 200


@pytest.mark.tier0
def test_ollama_config_admin_only(authenticated_user):
    resp = authenticated_user.get("/ollama/config")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_ollama_config_admin_access(authenticated_admin):
    resp = authenticated_admin.get("/ollama/config")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# T-328: OpenAI proxy
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_openai_config_admin_only(authenticated_user):
    resp = authenticated_user.get("/openai/config")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_openai_config_admin_access(authenticated_admin):
    resp = authenticated_admin.get("/openai/config")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# T-329: Llamolotl proxy
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_llamolotl_status_unauthenticated(client):
    """GET /llamolotl/ is a health check returning 200 without auth."""
    resp = client.get("/llamolotl/")
    assert resp.status_code == 200


@pytest.mark.tier0
def test_llamolotl_config_admin_only(authenticated_user):
    resp = authenticated_user.get("/llamolotl/config")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_llamolotl_config_admin_access(authenticated_admin):
    resp = authenticated_admin.get("/llamolotl/config")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# T-330: Curator proxy
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_curator_config_admin_only(authenticated_user):
    resp = authenticated_user.get("/curator/config")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_curator_config_admin_access(authenticated_admin):
    """Curator config read only hits app state — no upstream call."""
    resp = authenticated_admin.get("/curator/config")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# T-331: Audio (STT/TTS)
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_audio_config_admin_only(authenticated_user):
    resp = authenticated_user.get("/api/v1/audio/config")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_audio_config_admin_access(authenticated_admin):
    resp = authenticated_admin.get("/api/v1/audio/config")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# T-332: Images
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_images_config_admin_only(authenticated_user):
    resp = authenticated_user.get("/api/v1/images/config")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_images_config_admin_access(authenticated_admin):
    resp = authenticated_admin.get("/api/v1/images/config")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# T-333: Retrieval (config moved behind auth in T-205 fixes)
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_retrieval_status_requires_admin(authenticated_user):
    """Retrieval status was moved behind admin auth in security fixes."""
    resp = authenticated_user.get("/api/v1/retrieval/")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_retrieval_status_admin_access(authenticated_admin):
    resp = authenticated_admin.get("/api/v1/retrieval/")
    assert resp.status_code == 200


@pytest.mark.tier0
def test_retrieval_config_admin_only(authenticated_user):
    resp = authenticated_user.get("/api/v1/retrieval/config")
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# T-334: Eval proxies (language-eval + code-eval)
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_language_eval_config_admin_only(authenticated_user):
    resp = authenticated_user.get("/language-eval/config")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_language_eval_config_admin_access(authenticated_admin):
    resp = authenticated_admin.get("/language-eval/config")
    assert resp.status_code == 200


@pytest.mark.tier0
def test_code_eval_config_admin_only(authenticated_user):
    resp = authenticated_user.get("/code-eval/config")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_code_eval_config_admin_access(authenticated_admin):
    resp = authenticated_admin.get("/code-eval/config")
    assert resp.status_code == 200
