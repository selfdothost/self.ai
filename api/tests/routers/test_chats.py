"""
T-300 + T-301: Chats router CRUD, pagination, and import/export/clone tests.

Covers: list, create, read, update, delete, pagination, not-found,
cross-user isolation, import/export round-trip, clone.
"""

import pytest


# ---------------------------------------------------------------------------
# T-300: Basic CRUD + pagination + ownership
# ---------------------------------------------------------------------------

@pytest.mark.tier0
def test_list_chats_empty(authenticated_user):
    """Empty state: user with no chats gets empty list."""
    resp = authenticated_user.get("/api/v1/chats/")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.tier0
def test_create_chat_round_trip(authenticated_user):
    """Create returns the chat with the expected shape."""
    resp = authenticated_user.post(
        "/api/v1/chats/new",
        json={"chat": {"title": "Test Chat", "messages": []}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "id" in body
    assert body["chat"]["title"] == "Test Chat"


@pytest.mark.tier0
def test_create_then_list(authenticated_user):
    """Created chat appears in the list."""
    authenticated_user.post(
        "/api/v1/chats/new",
        json={"chat": {"title": "Listed Chat", "messages": []}},
    )
    resp = authenticated_user.get("/api/v1/chats/")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) >= 1
    assert any(c.get("title") == "Listed Chat" for c in body)


@pytest.mark.tier0
def test_get_chat_by_id_found(authenticated_user):
    """Get by id returns the created chat."""
    created = authenticated_user.post(
        "/api/v1/chats/new",
        json={"chat": {"title": "Specific Chat", "messages": []}},
    ).json()
    chat_id = created["id"]
    resp = authenticated_user.get(f"/api/v1/chats/{chat_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == chat_id
    assert resp.json()["chat"]["title"] == "Specific Chat"


@pytest.mark.tier0
def test_get_chat_by_id_not_found(authenticated_user):
    """Get with bogus id returns not-found indicator (null body or 404)."""
    resp = authenticated_user.get("/api/v1/chats/00000000-nonexistent")
    # Acceptable: 200 with null, 401 (access prohibited to non-owned), or 404
    if resp.status_code == 200:
        assert resp.json() is None or resp.json() == {}
    else:
        assert resp.status_code in (401, 404)


@pytest.mark.tier0
def test_update_chat(authenticated_user):
    """Update modifies the chat payload."""
    created = authenticated_user.post(
        "/api/v1/chats/new",
        json={"chat": {"title": "Old Title", "messages": []}},
    ).json()
    chat_id = created["id"]
    resp = authenticated_user.post(
        f"/api/v1/chats/{chat_id}",
        json={"chat": {"title": "New Title", "messages": []}},
    )
    assert resp.status_code == 200
    # Re-fetch to verify persistence
    refetch = authenticated_user.get(f"/api/v1/chats/{chat_id}").json()
    assert refetch["chat"]["title"] == "New Title"


@pytest.mark.tier0
def test_delete_chat_own(authenticated_user):
    """User can delete own chat."""
    created = authenticated_user.post(
        "/api/v1/chats/new",
        json={"chat": {"title": "Delete Me", "messages": []}},
    ).json()
    chat_id = created["id"]
    resp = authenticated_user.delete(f"/api/v1/chats/{chat_id}")
    assert resp.status_code == 200
    # After delete, listing should not contain it
    listing = authenticated_user.get("/api/v1/chats/").json()
    assert not any(c.get("id") == chat_id for c in listing)


@pytest.mark.tier0
def test_list_pagination(authenticated_user):
    """List endpoint accepts a page parameter without crashing."""
    # Create a few chats
    for i in range(3):
        authenticated_user.post(
            "/api/v1/chats/new",
            json={"chat": {"title": f"Paged {i}", "messages": []}},
        )
    resp = authenticated_user.get("/api/v1/chats/?page=1")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.tier0
def test_user_a_cannot_read_user_b_chat(authenticated_user, db_session):
    """User A can't read a chat owned by user B (cross-user isolation)."""
    from tests.factories import UserFactory, ChatFactory

    user_b = UserFactory.create(db_session)
    chat_b = ChatFactory.create(
        db_session, user_id=user_b.id, title="User B Private"
    )
    resp = authenticated_user.get(f"/api/v1/chats/{chat_b.id}")
    # Either 401/403/404 (denied or not-found-scoped) — all acceptable
    if resp.status_code == 200:
        assert resp.json() is None, "User A leaked user B's chat"
    else:
        assert resp.status_code in (401, 403, 404)


@pytest.mark.tier0
def test_user_a_list_excludes_user_b_chats(authenticated_user, db_session):
    """User A's list does not include user B's chats."""
    from tests.factories import UserFactory, ChatFactory

    user_b = UserFactory.create(db_session)
    ChatFactory.create(db_session, user_id=user_b.id, title="B's Chat")
    resp = authenticated_user.get("/api/v1/chats/")
    assert resp.status_code == 200
    titles = [c.get("title") for c in resp.json()]
    assert "B's Chat" not in titles


# ---------------------------------------------------------------------------
# T-301: Import/export/clone
# ---------------------------------------------------------------------------

@pytest.mark.tier0
def test_import_chat(authenticated_user):
    """Imported chat is stored and retrievable."""
    resp = authenticated_user.post(
        "/api/v1/chats/import",
        json={
            "chat": {"title": "Imported", "messages": []},
            "meta": {"tags": []},
            "pinned": False,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body is not None
    assert body["chat"]["title"] == "Imported"


@pytest.mark.tier0
def test_export_all_chats(authenticated_user):
    """/all endpoint returns all user's chats with full payload."""
    authenticated_user.post(
        "/api/v1/chats/new",
        json={"chat": {"title": "Exported", "messages": []}},
    )
    resp = authenticated_user.get("/api/v1/chats/all")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
    # Each entry should have chat content, not just id+title
    for entry in resp.json():
        assert "chat" in entry


@pytest.mark.tier0
def test_clone_chat(authenticated_user):
    """Cloning a chat produces a new chat with a different id."""
    source = authenticated_user.post(
        "/api/v1/chats/new",
        json={"chat": {
            "title": "Original",
            "messages": [],
            "history": {"currentId": "msg-1", "messages": {}},
        }},
    ).json()
    resp = authenticated_user.post(f"/api/v1/chats/{source['id']}/clone", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body is not None
    assert body["id"] != source["id"]
