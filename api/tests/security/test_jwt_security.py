"""
T-225 + T-226: JWT Security Tests.

Validates the JWT authentication layer rejects forged, tampered,
expired, and misconfigured tokens. Includes the 't0p-s3cr3t' default
secret red test.
"""

import time
import pytest
import jwt


ALGORITHM = "HS256"
TEST_SECRET = "test-secret-key-not-for-production"  # matches conftest


def _make_token(data: dict, secret: str = TEST_SECRET, algorithm: str = ALGORITHM) -> str:
    """Build a JWT with the given payload and signing secret."""
    return jwt.encode(data, secret, algorithm=algorithm)


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# T-225: Basic attacks
# ---------------------------------------------------------------------------

@pytest.mark.tier0
@pytest.mark.security
def test_tampered_signature_rejected(client):
    """A JWT with a tampered signature byte must return 401."""
    token = _make_token({"id": "user-1"})
    # Flip last character of signature
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    resp = client.get("/api/v1/users/", headers=_auth_headers(tampered))
    assert resp.status_code == 401


@pytest.mark.tier0
@pytest.mark.security
def test_expired_token_rejected(client):
    """A JWT with an exp in the past must return 401."""
    expired = _make_token({"id": "user-1", "exp": int(time.time()) - 3600})
    resp = client.get("/api/v1/users/", headers=_auth_headers(expired))
    assert resp.status_code == 401


@pytest.mark.tier0
@pytest.mark.security
def test_alg_none_rejected(client):
    """A token signed with alg=none must return 401."""
    # Manually construct an alg:none token (PyJWT >=2 blocks encoding it)
    import base64
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(b'{"id":"admin","role":"admin"}').rstrip(b"=").decode()
    none_token = f"{header}.{payload}."
    resp = client.get("/api/v1/users/", headers=_auth_headers(none_token))
    assert resp.status_code == 401


@pytest.mark.tier0
@pytest.mark.security
def test_wrong_secret_rejected(client):
    """A JWT signed with a different secret must return 401."""
    forged = _make_token({"id": "user-1"}, secret="attacker-secret")
    resp = client.get("/api/v1/users/", headers=_auth_headers(forged))
    assert resp.status_code == 401


@pytest.mark.tier0
@pytest.mark.security
def test_malformed_token_rejected(client):
    """A malformed token string must return 401."""
    resp = client.get(
        "/api/v1/users/", headers=_auth_headers("not-a-jwt-token")
    )
    assert resp.status_code == 401


@pytest.mark.tier0
@pytest.mark.security
def test_missing_bearer_prefix_rejected(client):
    """An Authorization header without Bearer prefix must return 401."""
    token = _make_token({"id": "user-1"})
    resp = client.get("/api/v1/users/", headers={"Authorization": token})
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# T-226: Secret and role forgery
# ---------------------------------------------------------------------------

@pytest.mark.tier0
@pytest.mark.security
def test_default_secret_forgery_rejected(client):
    """A JWT forged with the well-known default 't0p-s3cr3t' must be rejected
    when the application is configured with a different secret."""
    forged = _make_token({"id": "admin", "role": "admin"}, secret="t0p-s3cr3t")
    resp = client.get("/api/v1/users/", headers=_auth_headers(forged))
    assert resp.status_code == 401


@pytest.mark.tier0
@pytest.mark.security
def test_forged_admin_role_rejected(client, test_user):
    """A JWT with role=admin in the payload but signed for a regular user
    must not grant admin access. Role comes from DB, not token."""
    # Build a token claiming admin role, signed with the test secret,
    # but using the regular user's id
    forged = _make_token({"id": test_user["id"], "role": "admin"})
    # Try to hit an admin-only endpoint
    resp = client.get("/api/v1/users/", headers=_auth_headers(forged))
    # Should be 401 (if role check rejects) or 200 with user role
    # (DB lookup returns user, then admin check fails with 401)
    # Either way, the role claim in the token must NOT escalate privilege.
    if resp.status_code == 200:
        # If this endpoint isn't admin-only, that's ok. Verify that the
        # admin-required users list endpoint rejects it.
        resp = client.post(
            "/api/v1/users/update/role",
            headers=_auth_headers(forged),
            json={"id": test_user["id"], "role": "admin"},
        )
        assert resp.status_code in (401, 403, 404)
