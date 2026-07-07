"""
T-231: Role escalation prevention tests.

Validates that a user cannot escalate their own role via any endpoint
that accepts user data with a `role` field.
"""

import pytest


@pytest.mark.tier0
@pytest.mark.security
def test_user_cannot_self_escalate_via_user_info_update(
    authenticated_user, test_user, db_session
):
    """User submitting info update with role=admin does not become admin."""
    resp = authenticated_user.post(
        "/api/v1/users/user/info/update",
        json={"role": "admin", "permissions": ["all"]},
    )
    # Endpoint may return 200 or 422 — either way, role must not change
    from sqlalchemy import text
    row = db_session.execute(
        text("SELECT role FROM [user] WHERE id = :id"),
        {"id": test_user["id"]},
    ).fetchone()
    assert row is not None
    assert row[0] == "user", (
        f"User role was escalated to {row[0]} via info update"
    )


@pytest.mark.tier0
@pytest.mark.security
def test_user_cannot_escalate_via_settings(
    authenticated_user, test_user, db_session
):
    """User submitting settings update with role=admin does not become admin."""
    resp = authenticated_user.post(
        "/api/v1/users/user/settings/update",
        json={"role": "admin", "ui": {"admin": True}},
    )
    from sqlalchemy import text
    row = db_session.execute(
        text("SELECT role FROM [user] WHERE id = :id"),
        {"id": test_user["id"]},
    ).fetchone()
    if row:
        assert row[0] == "user", (
            f"User role was escalated to {row[0]} via settings"
        )


@pytest.mark.tier0
@pytest.mark.security
def test_user_cannot_escalate_via_profile_update(
    authenticated_user, test_user, db_session
):
    """User submitting profile update with role=admin does not become admin."""
    resp = authenticated_user.post(
        "/api/v1/auths/update/profile",
        json={
            "name": "Hacker",
            "profile_image_url": "/evil.png",
            "role": "admin",
        },
    )
    from sqlalchemy import text
    row = db_session.execute(
        text("SELECT role FROM [user] WHERE id = :id"),
        {"id": test_user["id"]},
    ).fetchone()
    if row:
        assert row[0] == "user", (
            f"User role was escalated to {row[0]} via profile update"
        )


@pytest.mark.tier0
@pytest.mark.security
def test_user_cannot_call_admin_role_update(authenticated_user, test_user):
    """The admin role-update endpoint must reject user-role callers."""
    resp = authenticated_user.post(
        "/api/v1/users/update/role",
        json={"id": test_user["id"], "role": "admin"},
    )
    assert resp.status_code in (401, 403), (
        f"User-role caller reached admin role-update endpoint: {resp.status_code}"
    )
