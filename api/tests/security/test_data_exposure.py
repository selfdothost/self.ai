"""
T-234 + T-235: Sensitive data exposure tests.

Validates that unauthenticated responses and error responses do not leak
secrets, internal paths, or configuration details.
"""

from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# T-234: Unauthenticated /api/config leaks
# ---------------------------------------------------------------------------

SENSITIVE_KEYS = [
    "api_key",
    "secret",
    "password",
    "jwt",
    "token",
    "private",
    "credential",
    "aws",
    "postgres",
]


def _contains_sensitive(obj, path="") -> list[str]:
    """Recursively scan a dict/list for keys that look like secrets."""
    findings = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            key_lower = str(k).lower()
            # Allow public config fields by name
            if key_lower in ("google_drive",):
                # google_drive SHOULD NOT appear unauthenticated — this is
                # the whole point of T-205. The presence of this key
                # itself is the issue.
                findings.append(f"{path}.{k}")
                continue
            if any(s in key_lower for s in SENSITIVE_KEYS):
                # Skip boolean flags (e.g. enable_api_key) — these are just
                # feature toggles, not actual secrets
                if v and v != "" and not isinstance(v, bool):
                    findings.append(f"{path}.{k}={v!r}")
            if isinstance(v, (dict, list)):
                findings.extend(_contains_sensitive(v, f"{path}.{k}"))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, (dict, list)):
                findings.extend(_contains_sensitive(v, f"{path}[{i}]"))
    return findings


@pytest.mark.tier0
@pytest.mark.security
def test_unauthenticated_config_has_no_google_drive_keys(client):
    """Unauthenticated /api/config must not return google_drive credentials."""
    resp = client.get("/api/config")
    assert resp.status_code == 200
    body = resp.json()
    assert "google_drive" not in body, (
        "Unauthenticated /api/config response must not include google_drive. "
        "It should only appear when the caller is authenticated."
    )


@pytest.mark.tier0
@pytest.mark.security
def test_unauthenticated_config_no_obvious_secrets(client):
    """Unauthenticated /api/config must not include API keys or secrets."""
    resp = client.get("/api/config")
    assert resp.status_code == 200
    body = resp.json()
    findings = _contains_sensitive(body)
    assert findings == [], f"Unauthenticated /api/config exposes sensitive keys: {findings}"


# ---------------------------------------------------------------------------
# T-235: Error responses don't leak stack traces
# ---------------------------------------------------------------------------


@pytest.mark.tier0
@pytest.mark.security
def test_error_response_no_stack_trace(client):
    """Error responses must not contain Python stack trace indicators."""
    # Trigger an error by sending malformed JSON to an endpoint
    resp = client.post(
        "/api/v1/auths/signin",
        content="not-valid-json",
        headers={"Content-Type": "application/json"},
    )
    # Any 4xx is fine; we just don't want stack traces
    body = resp.text
    stack_indicators = [
        "Traceback",
        'File "/',
        "line ",
        "selfai_ui/",
        '.py", line',
    ]
    for indicator in stack_indicators:
        assert indicator not in body, f"Error response contains stack trace indicator '{indicator}':\n{body[:500]}"


@pytest.mark.tier0
@pytest.mark.security
def test_401_no_mechanism_hints(client):
    """401/403 error responses must not reveal the expected auth mechanism."""
    resp = client.get("/api/v1/users/")
    assert resp.status_code in (401, 403)
    body = resp.text.lower()
    # Should not give explicit hints like "Bearer token required" or
    # "JWT expired" that help an attacker learn what's expected
    # (generic "unauthorized" / "not authenticated" is fine)
    sensitive_phrases = [
        "bearer token required",
        "jwt expired",
        "jwt malformed",
        "api key required",
    ]
    for phrase in sensitive_phrases:
        assert phrase not in body, f"401/403 response leaks auth mechanism hint: '{phrase}'"


# ---------------------------------------------------------------------------
# Issue #6: Unauthenticated /api/models/public leaks
# ---------------------------------------------------------------------------


def _create_model(admin_client, model_id: str, access_control=None, base_model_id="test-base-model"):
    """Create a model as admin, then return the client with auth stripped

    so the caller can immediately exercise the unauthenticated endpoint
    against the same TestClient/app/db.
    """
    payload = {
        "id": model_id,
        "base_model_id": base_model_id,
        "name": f"Model {model_id}",
        "meta": {
            "description": "test",
            "hf_repo": "org/should-not-leak",
        },
        "params": {},
        "access_control": access_control,
        "is_active": True,
    }
    resp = admin_client.post("/api/v1/models/create", json=payload)
    assert resp.status_code == 200, resp.text
    admin_client.headers.pop("Authorization", None)
    return admin_client


def _mock_base_models(*model_ids):
    """Build an async callable that stands in for `get_all_models`, returning
    a bare provider-shaped model list (no `info`/lineage) for the given ids.

    `/api/models/public`'s own access-control filtering is what's under
    test here, not provider connectivity -- in the test env no
    openai/ollama/llamolotl backend actually responds, and
    `get_all_models()` short-circuits to `[]` when there are zero base
    models, which would otherwise make these tests depend on live network
    access instead of the endpoint's filtering logic.
    """

    async def _fake(request):
        return [
            {
                "id": model_id,
                "name": model_id,
                "object": "model",
                "created": 1234567890,
                "owned_by": "openai",
            }
            for model_id in model_ids
        ]

    return _fake


@pytest.mark.tier0
@pytest.mark.security
def test_public_models_no_auth_required(client):
    """/api/models/public must be reachable without any credentials."""
    resp = client.get("/api/models/public")
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body
    assert isinstance(body["data"], list)


@pytest.mark.tier0
@pytest.mark.security
def test_public_models_includes_public_model(authenticated_admin):
    """A model with access_control=None (public) shows up in the free-tier list."""
    anon = _create_model(authenticated_admin, "public-free-model", access_control=None)

    with patch("selfai_ui.main.get_all_models", new=_mock_base_models("public-free-model")):
        resp = anon.get("/api/models/public")
    assert resp.status_code == 200
    ids = [m["id"] for m in resp.json()["data"]]
    assert "public-free-model" in ids


@pytest.mark.tier0
@pytest.mark.security
def test_public_models_excludes_restricted_model(authenticated_admin):
    """A model with a non-null access_control (private/scoped) must not appear."""
    anon = _create_model(authenticated_admin, "restricted-model", access_control={})

    with patch("selfai_ui.main.get_all_models", new=_mock_base_models("restricted-model")):
        resp = anon.get("/api/models/public")
    assert resp.status_code == 200
    ids = [m["id"] for m in resp.json()["data"]]
    assert "restricted-model" not in ids


@pytest.mark.tier0
@pytest.mark.security
def test_public_models_no_sensitive_fields(authenticated_admin):
    """The public endpoint must not expose info/meta lineage, user, or connection data."""
    anon = _create_model(authenticated_admin, "public-shape-model", access_control=None)

    with patch("selfai_ui.main.get_all_models", new=_mock_base_models("public-shape-model")):
        resp = anon.get("/api/models/public")
    assert resp.status_code == 200
    body = resp.json()

    entry = next(m for m in body["data"] if m["id"] == "public-shape-model")
    # Exactly this flat key set -- no "info" (params/meta lineage), no
    # per-backend raw blob ("llamolotl"/"ollama"/"openai" keys), no
    # "user"/"user_id", no access_control.
    assert set(entry.keys()) == {"id", "name", "object", "created", "owned_by"}

    # The hf_repo lineage value from ModelMeta must not leak anywhere.
    assert "should-not-leak" not in resp.text, "hf_repo lineage leaked into public model listing"

    findings = _contains_sensitive(body)
    assert findings == [], f"/api/models/public exposes sensitive keys: {findings}"
