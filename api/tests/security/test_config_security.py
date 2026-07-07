"""
T-232 + T-233: Configuration security tests.

Validates that security-relevant configuration defaults are correctly enforced:
- Default JWT secret is blocked on startup
- CORS policy rejects wildcard with credentials
- Default security headers are present in responses
- Session cookie has secure flag in non-debug mode
"""

import os
import subprocess
import sys
import pytest


# ---------------------------------------------------------------------------
# T-232: Default JWT secret blocked on startup
# ---------------------------------------------------------------------------

@pytest.mark.tier0
@pytest.mark.security
def test_default_jwt_secret_blocked_on_startup():
    """Startup must fail when WEBUI_SECRET_KEY is the well-known default."""
    env = os.environ.copy()
    env["WEBUI_SECRET_KEY"] = "t0p-s3cr3t"
    env["WEBUI_AUTH"] = "True"
    env["ENV"] = "prod"

    result = subprocess.run(
        [sys.executable, "-c", "import selfai_ui.env"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0, (
        "Import should fail when WEBUI_SECRET_KEY is the default 't0p-s3cr3t'"
    )
    assert "t0p-s3cr3t" in (result.stderr + result.stdout), (
        "Error message should mention the default secret"
    )


@pytest.mark.tier0
@pytest.mark.security
def test_empty_jwt_secret_blocked_on_startup():
    """Startup must also fail when WEBUI_SECRET_KEY is empty."""
    env = os.environ.copy()
    env["WEBUI_SECRET_KEY"] = ""
    env["WEBUI_AUTH"] = "True"
    env["ENV"] = "prod"

    result = subprocess.run(
        [sys.executable, "-c", "import selfai_ui.env"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0, "Import should fail with empty secret"


# ---------------------------------------------------------------------------
# T-233: Security headers, CORS, cookie flags
# ---------------------------------------------------------------------------

@pytest.mark.tier0
@pytest.mark.security
def test_security_headers_present_in_responses(client):
    """Responses include the default security headers."""
    resp = client.get("/health")
    assert resp.status_code == 200
    headers_lower = {k.lower(): v for k, v in resp.headers.items()}
    assert headers_lower.get("x-content-type-options") == "nosniff"
    assert headers_lower.get("x-frame-options") == "DENY"
    assert "strict-origin" in headers_lower.get("referrer-policy", "").lower()


@pytest.mark.tier0
@pytest.mark.security
def test_cors_default_is_not_wildcard():
    """CORS default must not be wildcard when no env var is set."""
    from selfai_ui.config import CORS_ALLOW_ORIGIN
    # Default should be empty list (same-origin only), not wildcard
    assert "*" not in CORS_ALLOW_ORIGIN or CORS_ALLOW_ORIGIN == [], (
        f"CORS default should not include '*' by default. Got: {CORS_ALLOW_ORIGIN}"
    )


@pytest.mark.tier0
@pytest.mark.security
def test_cookie_secure_default_in_non_dev():
    """Session cookie Secure flag must be True when not in dev mode."""
    env = os.environ.copy()
    env["WEBUI_SECRET_KEY"] = "test-secret"
    env["WEBUI_AUTH"] = "True"
    env["ENV"] = "prod"
    env.pop("WEBUI_SESSION_COOKIE_SECURE", None)

    result = subprocess.run(
        [sys.executable, "-c",
         "import selfai_ui.env; "
         "print('COOKIE_SECURE=' + str(selfai_ui.env.WEBUI_SESSION_COOKIE_SECURE))"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"Import failed: {result.stderr}"
    assert "COOKIE_SECURE=True" in result.stdout, (
        f"Cookie secure should default to True in non-dev. Got: {result.stdout[-500:]}"
    )


@pytest.mark.tier0
@pytest.mark.security
def test_cookie_secure_default_in_dev():
    """Session cookie Secure flag defaults to False in dev mode."""
    env = os.environ.copy()
    env["WEBUI_SECRET_KEY"] = "test-secret"
    env["WEBUI_AUTH"] = "True"
    env["ENV"] = "dev"
    env.pop("WEBUI_SESSION_COOKIE_SECURE", None)

    result = subprocess.run(
        [sys.executable, "-c",
         "import selfai_ui.env; "
         "print('COOKIE_SECURE=' + str(selfai_ui.env.WEBUI_SESSION_COOKIE_SECURE))"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "COOKIE_SECURE=False" in result.stdout, (
        f"Cookie secure should default to False in dev. Got: {result.stdout[-500:]}"
    )
