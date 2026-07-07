"""
T-223 + T-224: Authorization matrix tests.

Vertical escalation: anonymous/user roles cannot access admin endpoints.
Horizontal isolation: user A cannot access user B's resources.
"""

import pytest


# ---------------------------------------------------------------------------
# T-223: Vertical escalation (role × endpoint matrix)
# ---------------------------------------------------------------------------

# Admin-only endpoints — anonymous and user roles should get 401/403
ADMIN_ONLY_ENDPOINTS = [
    ("GET", "/api/v1/users/"),
    ("POST", "/api/v1/groups/create"),
    ("POST", "/api/v1/configs/banners"),
    ("GET", "/api/system/resources"),
    ("GET", "/api/system/processes"),
    ("GET", "/api/queue"),
    ("POST", "/api/v1/functions/create"),
]


@pytest.mark.tier0
@pytest.mark.security
@pytest.mark.parametrize("method,endpoint", ADMIN_ONLY_ENDPOINTS)
def test_anonymous_cannot_access_admin_endpoint(client, method, endpoint):
    """Anonymous requests to admin endpoints must be rejected."""
    resp = client.request(method, endpoint)
    assert resp.status_code in (401, 403), (
        f"Anonymous {method} {endpoint} returned {resp.status_code}, "
        f"expected 401 or 403"
    )


@pytest.mark.tier0
@pytest.mark.security
@pytest.mark.parametrize("method,endpoint", ADMIN_ONLY_ENDPOINTS)
def test_user_cannot_access_admin_endpoint(authenticated_user, method, endpoint):
    """User-role requests to admin endpoints must be rejected."""
    resp = authenticated_user.request(method, endpoint)
    assert resp.status_code in (401, 403), (
        f"User {method} {endpoint} returned {resp.status_code}, "
        f"expected 401 or 403"
    )


# Endpoints the user role should be able to access (just check non-auth errors)
USER_ACCESSIBLE_ENDPOINTS = [
    "/api/v1/auths/",
    "/api/v1/chats/",
    "/api/v1/knowledge/",
    "/api/v1/prompts/",
    "/api/v1/models",
]


@pytest.mark.tier0
@pytest.mark.security
@pytest.mark.parametrize("endpoint", USER_ACCESSIBLE_ENDPOINTS)
def test_user_can_access_user_endpoint(authenticated_user, endpoint):
    """User-role requests to user endpoints do not 401/403."""
    resp = authenticated_user.get(endpoint)
    # Not 401 or 403 — might be 200, 404, or something else based on state
    assert resp.status_code not in (401, 403), (
        f"User cannot access {endpoint}: got {resp.status_code}"
    )


@pytest.mark.tier0
@pytest.mark.security
def test_admin_can_access_admin_endpoints(authenticated_admin):
    """Admin-role requests to admin endpoints succeed."""
    resp = authenticated_admin.get("/api/v1/users/")
    assert resp.status_code not in (401, 403), (
        f"Admin cannot access /api/v1/users/: got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# T-224: Horizontal isolation (user A cannot read user B's data)
# ---------------------------------------------------------------------------

@pytest.mark.tier0
@pytest.mark.security
def test_user_cannot_read_other_user_chat(authenticated_user, db_session):
    """User A cannot read user B's chat by guessing chat ID."""
    from tests.factories import UserFactory, ChatFactory

    user_b = UserFactory.create(db_session)
    chat_b = ChatFactory.create(db_session, user_id=user_b.id)

    resp = authenticated_user.get(f"/api/v1/chats/{chat_b.id}")
    # Should be 401, 403, or 404 (either denied or indistinguishable from not-found)
    assert resp.status_code in (401, 403, 404), (
        f"User accessed another user's chat: {resp.status_code}"
    )


@pytest.mark.tier0
@pytest.mark.security
def test_user_cannot_delete_other_user_chat(authenticated_user, db_session):
    """User A's delete of user B's chat is a no-op (chat still exists)."""
    from tests.factories import UserFactory, ChatFactory
    from sqlalchemy import text

    user_b = UserFactory.create(db_session)
    chat_b = ChatFactory.create(db_session, user_id=user_b.id)
    chat_id = chat_b.id

    authenticated_user.delete(f"/api/v1/chats/{chat_id}")
    # Status may be 200 (returns False) or 401/403/404 — either is fine.
    # The important thing: the chat must still exist in the DB.
    row = db_session.execute(
        text("SELECT id FROM chat WHERE id = :id"), {"id": chat_id}
    ).fetchone()
    assert row is not None, (
        "User A's DELETE actually removed user B's chat — horizontal "
        "isolation is broken!"
    )


@pytest.mark.tier0
@pytest.mark.security
def test_user_cannot_list_other_users_chats(authenticated_user, db_session, test_user):
    """User A listing chats only sees their own."""
    from tests.factories import UserFactory, ChatFactory

    user_b = UserFactory.create(db_session)
    # Create a chat for user B
    ChatFactory.create(db_session, user_id=user_b.id, title="User B's Private Chat")

    resp = authenticated_user.get("/api/v1/chats/")
    if resp.status_code == 200:
        body = resp.json()
        titles = [c.get("title", "") for c in body] if isinstance(body, list) else []
        assert "User B's Private Chat" not in titles, (
            "User A's chat list includes user B's chats"
        )
