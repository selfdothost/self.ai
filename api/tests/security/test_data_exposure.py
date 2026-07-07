"""
T-234 + T-235: Sensitive data exposure tests.

Validates that unauthenticated responses and error responses do not leak
secrets, internal paths, or configuration details.
"""

import pytest


# ---------------------------------------------------------------------------
# T-234: Unauthenticated /api/config leaks
# ---------------------------------------------------------------------------

SENSITIVE_KEYS = [
    "api_key", "secret", "password", "jwt", "token",
    "private", "credential", "aws", "postgres",
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
    assert findings == [], (
        f"Unauthenticated /api/config exposes sensitive keys: {findings}"
    )


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
        "File \"/",
        "line ",
        "selfai_ui/",
        ".py\", line",
    ]
    for indicator in stack_indicators:
        assert indicator not in body, (
            f"Error response contains stack trace indicator '{indicator}':\n{body[:500]}"
        )


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
        assert phrase not in body, (
            f"401/403 response leaks auth mechanism hint: '{phrase}'"
        )
