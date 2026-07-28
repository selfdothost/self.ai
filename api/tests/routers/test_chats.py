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
    from tests.factories import ChatFactory, UserFactory

    user_b = UserFactory.create(db_session)
    chat_b = ChatFactory.create(db_session, user_id=user_b.id, title="User B Private")
    resp = authenticated_user.get(f"/api/v1/chats/{chat_b.id}")
    # Either 401/403/404 (denied or not-found-scoped) — all acceptable
    if resp.status_code == 200:
        assert resp.json() is None, "User A leaked user B's chat"
    else:
        assert resp.status_code in (401, 403, 404)


@pytest.mark.tier0
def test_user_a_list_excludes_user_b_chats(authenticated_user, db_session):
    """User A's list does not include user B's chats."""
    from tests.factories import ChatFactory, UserFactory

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
        json={
            "chat": {
                "title": "Original",
                "messages": [],
                "history": {"currentId": "msg-1", "messages": {}},
            }
        },
    ).json()
    resp = authenticated_user.post(f"/api/v1/chats/{source['id']}/clone", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body is not None
    assert body["id"] != source["id"]


# ===========================================================================
# Cavekit: chat-list-unfoldered-filter
# Build site: context/plans/build-site-chat-list-unfoldered-filter.md
# Regression-guard tests locking in the unfoldered-only recent-chats contract.
# ===========================================================================
#
# T-001: folder-assignment test scaffolding (Cavekit R1).
#
# Shared arrange helpers used by every downstream unfoldered-filter test to put
# chats into and out of folders. Assignment is done via the pre-existing
# `POST /{id}/folder` route (`update_chat_folder_id_by_id`), which this kit does
# NOT modify — it is exercised only as test setup (kit Out of Scope).
#
# The recent-chats list route `GET /` (+ `/list` alias) returns
# ChatTitleIdResponse items {id, title, updated_at, created_at} — there is no
# folder_id field on the response, so foldered/unfoldered state is asserted by
# presence/absence of a chat's id in the list, not by inspecting a field.


def _create_chat(client, title):
    """Create a chat via the API; return its id."""
    resp = client.post(
        "/api/v1/chats/new",
        json={"chat": {"title": title, "messages": []}},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _create_folder(client, name, parent_id=None):
    """Create a folder via the API; return its id. Optionally reparent it under
    parent_id using the proven `/update/parent` route."""
    resp = client.post("/api/v1/folders/", json={"name": name})
    assert resp.status_code == 200, resp.text
    folder_id = resp.json()["id"]
    if parent_id is not None:
        resp = client.post(
            f"/api/v1/folders/{folder_id}/update/parent",
            json={"parent_id": parent_id},
        )
        assert resp.status_code == 200, resp.text
    return folder_id


def _assign_chat_to_folder(client, chat_id, folder_id):
    """Assign a chat to a folder via POST /{id}/folder (setup only)."""
    resp = client.post(
        f"/api/v1/chats/{chat_id}/folder",
        json={"folder_id": folder_id},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _clear_chat_folder(client, chat_id):
    """Clear a chat's folder assignment (folder_id -> null) via POST /{id}/folder."""
    resp = client.post(
        f"/api/v1/chats/{chat_id}/folder",
        json={"folder_id": None},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _recent_chat_ids(client, page=None):
    """Fetch the recent-chats list (GET / or GET /?page=N) and return ids in
    the order returned."""
    url = "/api/v1/chats/"
    if page is not None:
        url += f"?page={page}"
    resp = client.get(url)
    assert resp.status_code == 200, resp.text
    return [c["id"] for c in resp.json()]


@pytest.mark.tier0
def test_scaffolding_assign_and_clear_folder(authenticated_user):
    """T-001: the shared arrange helpers work end to end.

    Proves: a chat can be created, assigned to a folder (response carries the
    folder_id), and cleared back to null (response folder_id is null). This is
    the setup primitive every unfoldered-filter contract test relies on.
    """
    chat_id = _create_chat(authenticated_user, "Scaffold Chat")
    folder_id = _create_folder(authenticated_user, "Scaffold Folder")

    assigned = _assign_chat_to_folder(authenticated_user, chat_id, folder_id)
    assert assigned["folder_id"] == folder_id

    cleared = _clear_chat_folder(authenticated_user, chat_id)
    assert cleared["folder_id"] is None


# ---------------------------------------------------------------------------
# T-002: GET / (and /list alias) returns unfoldered-only, unconditionally.
# Covers R1 AC1 (foldered chat never appears) + AC4 (no request parameter is
# required for, or can defeat, unfoldered-only).
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_recent_list_excludes_foldered_chat(authenticated_user):
    """R1 AC1: a chat assigned to a folder never appears in the recent list.

    Backed by get_chat_title_id_list_by_user_id, which filters
    `.filter_by(folder_id=None)` (models/chats.py:390) before any pagination.
    """
    unfoldered_id = _create_chat(authenticated_user, "Unfoldered Recent")
    foldered_id = _create_chat(authenticated_user, "To Be Foldered")
    folder_id = _create_folder(authenticated_user, "R1 Folder")
    _assign_chat_to_folder(authenticated_user, foldered_id, folder_id)

    ids = _recent_chat_ids(authenticated_user)
    assert unfoldered_id in ids, "unfoldered chat missing from recent list"
    assert foldered_id not in ids, "foldered chat leaked into recent list"


@pytest.mark.tier0
def test_list_alias_matches_root(authenticated_user):
    """R1: the `/list` alias returns the same unfoldered set as `/`.

    Both routes are bound to the same handler (routers/chats.py:32-33).
    """
    unfoldered_id = _create_chat(authenticated_user, "Alias Unfoldered")
    foldered_id = _create_chat(authenticated_user, "Alias Foldered")
    folder_id = _create_folder(authenticated_user, "Alias Folder")
    _assign_chat_to_folder(authenticated_user, foldered_id, folder_id)

    resp = authenticated_user.get("/api/v1/chats/list")
    assert resp.status_code == 200
    alias_ids = [c["id"] for c in resp.json()]
    assert unfoldered_id in alias_ids
    assert foldered_id not in alias_ids
    # Same membership as the root route.
    assert set(alias_ids) == set(_recent_chat_ids(authenticated_user))


@pytest.mark.tier0
def test_unfoldered_only_is_unconditional(authenticated_user):
    """R1 AC4: no request parameter is required for, or can defeat, the
    unfoldered-only behavior.

    The route accepts only `page` (routers/chats.py:34). There is no parameter
    to include foldered chats; unknown query params are ignored by FastAPI and
    do not flip the filter. Verified across the default path, the paginated
    path, and a request carrying speculative "include-foldered" params.
    """
    unfoldered_id = _create_chat(authenticated_user, "Uncond Unfoldered")
    foldered_id = _create_chat(authenticated_user, "Uncond Foldered")
    folder_id = _create_folder(authenticated_user, "Uncond Folder")
    _assign_chat_to_folder(authenticated_user, foldered_id, folder_id)

    for url in (
        "/api/v1/chats/",
        "/api/v1/chats/?page=1",
        # Speculative params that do not exist on the endpoint — must be ignored,
        # foldered chat must still be excluded.
        f"/api/v1/chats/?folder_id={folder_id}",
        "/api/v1/chats/?include_foldered=true",
        "/api/v1/chats/?all=true",
    ):
        resp = authenticated_user.get(url)
        assert resp.status_code == 200, f"{url} -> {resp.status_code}"
        ids = [c["id"] for c in resp.json()]
        assert foldered_id not in ids, f"foldered chat leaked via {url}"
        assert unfoldered_id in ids, f"unfoldered chat missing via {url}"


# ---------------------------------------------------------------------------
# T-003: assign/clear round-trip. Covers R1 AC2 (assigning removes from list on
# next read) + AC3 (clearing folder_id back to null returns it).
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_folder_assignment_round_trip(authenticated_user):
    """R1 AC2 + AC3: a chat's presence in the recent list tracks folder_id.

    1. Unfoldered chat is present.
    2. After assigning it to a folder -> absent on next read (AC2).
    3. After clearing folder_id back to null -> present again on next read (AC3).

    Each step re-reads GET / fresh, so the assertion is against the endpoint's
    live filter (folder_id=None, models/chats.py:390), not a cached response.
    """
    chat_id = _create_chat(authenticated_user, "Round Trip Chat")
    folder_id = _create_folder(authenticated_user, "Round Trip Folder")

    # 1. Initially unfoldered -> present.
    assert chat_id in _recent_chat_ids(authenticated_user)

    # 2. Assign to folder -> removed on next read (AC2).
    _assign_chat_to_folder(authenticated_user, chat_id, folder_id)
    assert chat_id not in _recent_chat_ids(authenticated_user)

    # 3. Clear folder_id back to null -> returns on next read (AC3).
    _clear_chat_folder(authenticated_user, chat_id)
    assert chat_id in _recent_chat_ids(authenticated_user)


# ---------------------------------------------------------------------------
# T-011: per-folder listing regression. Covers R4 AC3 — GET /folder/{folder_id}
# still returns that folder's chats AND its descendant folders' chats, unchanged
# by this work (the endpoint this kit must not regress).
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_folder_listing_returns_folder_and_descendant_chats(authenticated_user):
    """R4 AC3: GET /folder/{parent} returns chats in the parent folder and in
    its descendant (child) folder; an unrelated unfoldered chat is excluded.

    Router get_chats_by_folder_id (routers/chats.py:155) walks descendants via
    Folders.get_children_folders_by_id_and_user_id and passes the full id set to
    get_chats_by_folder_ids_and_user_id (models/chats.py:641, filters
    Chat.folder_id.in_(folder_ids)).
    """
    parent_id = _create_folder(authenticated_user, "Parent Folder")
    child_id = _create_folder(authenticated_user, "Child Folder", parent_id=parent_id)

    parent_chat = _create_chat(authenticated_user, "In Parent")
    child_chat = _create_chat(authenticated_user, "In Child")
    loose_chat = _create_chat(authenticated_user, "Unfoldered Loose")

    _assign_chat_to_folder(authenticated_user, parent_chat, parent_id)
    _assign_chat_to_folder(authenticated_user, child_chat, child_id)

    resp = authenticated_user.get(f"/api/v1/chats/folder/{parent_id}")
    assert resp.status_code == 200, resp.text
    ids = [c["id"] for c in resp.json()]

    assert parent_chat in ids, "parent-folder chat missing from per-folder listing"
    assert child_chat in ids, "descendant-folder chat missing from per-folder listing"
    assert loose_chat not in ids, "unfoldered chat leaked into per-folder listing"

    # And the loose (unfoldered) chat is exactly the one the recent list keeps.
    assert loose_chat in _recent_chat_ids(authenticated_user)
    assert parent_chat not in _recent_chat_ids(authenticated_user)
    assert child_chat not in _recent_chat_ids(authenticated_user)


# ---------------------------------------------------------------------------
# T-013: re-verification gate (kit-wide grounding-note claim across R1-R4).
#
# Locks the three shared list-method bodies re-read against live code:
#   get_chat_list_by_user_id            (models/chats.py:360) folder_id=None
#   get_chat_title_id_list_by_user_id   (models/chats.py:382) folder_id=None
#   get_chats_by_folder_ids_and_user_id (models/chats.py:641) folder_id.in_(...)
#
# Re-read verdict: every R1-R4 assertion holds against the current tree; the
# folder filter is applied to the query BEFORE offset/limit in both list
# methods, so NO production change was required. T-012 stays dormant.
#
# This test exercises the methods directly (not via routes) so it fails loudly
# if a future refactor moves the folder filter after pagination or drops it.
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_shared_list_methods_filter_folder_before_pagination(test_user, db_session):
    """T-013: direct model-level lock on the folder filter + filter-before-pagination.

    Dataset (distinct updated_at for deterministic desc ordering):
      unfoldered: u_new > u_mid > u_old   (folder_id = None)
      foldered:   f_a (folder "fa"), f_b (folder "fb")  interleaved by recency
    """
    from selfai_ui.models.chats import Chats
    from tests.factories import ChatFactory

    uid = test_user["id"]
    base = 1_900_000_000  # fixed, well past any real seed timestamp

    u_new = ChatFactory.create(db_session, user_id=uid, title="u_new", folder_id=None, updated_at=base + 100)
    f_a = ChatFactory.create(db_session, user_id=uid, title="f_a", folder_id="fa", updated_at=base + 95)
    u_mid = ChatFactory.create(db_session, user_id=uid, title="u_mid", folder_id=None, updated_at=base + 90)
    f_b = ChatFactory.create(db_session, user_id=uid, title="f_b", folder_id="fb", updated_at=base + 85)
    u_old = ChatFactory.create(db_session, user_id=uid, title="u_old", folder_id=None, updated_at=base + 80)

    unfoldered_ids = [u_new.id, u_mid.id, u_old.id]
    foldered_ids = {f_a.id, f_b.id}

    # R1 / R4 AC1-AC2: both list methods return unfoldered-only, desc-ordered.
    title_id = Chats.get_chat_title_id_list_by_user_id(uid)
    got = [c.id for c in title_id]
    assert got == unfoldered_ids, f"title-id list not unfoldered-only/ordered: {got}"
    assert not (set(got) & foldered_ids)

    full = Chats.get_chat_list_by_user_id(uid)
    got_full = [c.id for c in full]
    assert got_full == unfoldered_ids, f"chat list not unfoldered-only/ordered: {got_full}"
    assert not (set(got_full) & foldered_ids)

    # R2 AC1-AC3: filter applied BEFORE pagination. If the folder filter ran
    # after the offset/limit window, f_a/f_b (which sort between the unfoldered
    # chats by recency) would consume page slots and u_old would be dropped.
    page1 = [c.id for c in Chats.get_chat_title_id_list_by_user_id(uid, skip=0, limit=2)]
    page2 = [c.id for c in Chats.get_chat_title_id_list_by_user_id(uid, skip=2, limit=2)]
    assert page1 == [u_new.id, u_mid.id], f"page1 leaked/shrank: {page1}"
    assert page2 == [u_old.id], f"page2 skipped/duplicated: {page2}"
    assert not (foldered_ids & (set(page1) | set(page2))), "foldered chat occupied a page slot"

    # R4 AC3: the per-folder method returns folder-scoped chats (the inverse set).
    fa_only = [c.id for c in Chats.get_chats_by_folder_ids_and_user_id(["fa"], uid)]
    assert fa_only == [f_a.id]
    both = {c.id for c in Chats.get_chats_by_folder_ids_and_user_id(["fa", "fb"], uid)}
    assert both == foldered_ids
    assert not (both & set(unfoldered_ids))


# ---------------------------------------------------------------------------
# T-004: route-level filter-before-pagination — a full page of unfoldered chats.
# Covers R2 AC1 (+ R3 AC4). A user with > one page of unfoldered chats gets a
# full page-1 of 60 unfoldered chats; foldered rows never consume page slots.
# ---------------------------------------------------------------------------

PAGE_LIMIT = 60  # routers/chats.py:35


def _bulk_chats(db_session, user_id, count, folder_id, base_updated_at):
    """Create `count` chats for user with distinct descending-friendly
    updated_at values; return their ids newest-first."""
    from tests.factories import ChatFactory

    ids = []
    for i in range(count):
        c = ChatFactory.create(
            db_session,
            user_id=user_id,
            title=f"{'F' if folder_id else 'U'}-{i}",
            folder_id=folder_id,
            updated_at=base_updated_at + i,
        )
        ids.append(c.id)
    ids.reverse()  # newest (largest updated_at) first
    return ids


@pytest.mark.tier0
def test_full_page_of_unfoldered_not_shrunk_by_foldered(authenticated_user, test_user, db_session):
    """R2 AC1: page-1 returns a full 60 unfoldered chats even though the user
    also has foldered chats with newer timestamps.

    65 unfoldered + 20 foldered (foldered given the newest timestamps, so a
    broken post-pagination filter would push unfoldered chats off page 1).
    """
    uid = test_user["id"]
    base = 1_900_000_000

    unfoldered_ids = _bulk_chats(db_session, uid, 65, None, base)
    # Foldered chats with strictly newer timestamps than every unfoldered chat.
    foldered_ids = set(_bulk_chats(db_session, uid, 20, "fa", base + 1000))

    resp = authenticated_user.get("/api/v1/chats/?page=1")
    assert resp.status_code == 200, resp.text
    page1 = [c["id"] for c in resp.json()]

    assert len(page1) == PAGE_LIMIT, f"page 1 not full: {len(page1)}"
    assert not (set(page1) & foldered_ids), "foldered chat occupied a page-1 slot"
    # Page 1 is exactly the 60 newest unfoldered chats, in order.
    assert page1 == unfoldered_ids[:PAGE_LIMIT]


# ---------------------------------------------------------------------------
# T-005: page-walk integrity. Covers R2 AC2 (no unfoldered skipped/duplicated
# across a page boundary) + AC3 (no foldered chat in any slot), also R3 AC4
# (page N is the Nth window of the filtered order).
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_page_walk_no_skip_or_duplicate_across_boundary(authenticated_user, test_user, db_session):
    """R2 AC2 + AC3: walking page 1 then page 2 covers every unfoldered chat
    exactly once and no foldered chat ever appears.

    70 unfoldered + 30 foldered, timestamps interleaved so a foldered chat sits
    on either side of the 60-item page boundary if the filter were wrong.
    """
    uid = test_user["id"]
    base = 1_900_000_000

    # Interleave: unfoldered at even offsets, foldered at odd offsets, so their
    # updated_at values alternate across the whole range (and the boundary).
    from tests.factories import ChatFactory

    unfoldered_ids = []
    foldered_ids = set()
    for i in range(100):
        if i % 10 < 7:  # 70 unfoldered, 30 foldered, interleaved by recency
            c = ChatFactory.create(db_session, user_id=uid, title=f"U{i}", folder_id=None, updated_at=base + i)
            unfoldered_ids.append(c.id)
        else:
            c = ChatFactory.create(db_session, user_id=uid, title=f"F{i}", folder_id="fa", updated_at=base + i)
            foldered_ids.add(c.id)
    unfoldered_ids.reverse()  # newest-first
    assert len(unfoldered_ids) == 70 and len(foldered_ids) == 30

    page1 = [c["id"] for c in authenticated_user.get("/api/v1/chats/?page=1").json()]
    page2 = [c["id"] for c in authenticated_user.get("/api/v1/chats/?page=2").json()]

    assert len(page1) == PAGE_LIMIT  # 60
    assert len(page2) == 10  # remaining unfoldered

    # AC3: no foldered chat in any slot of any page.
    assert not (foldered_ids & (set(page1) | set(page2)))

    # AC2: no duplicate across the boundary.
    assert not (set(page1) & set(page2)), "chat duplicated across page boundary"

    # AC2/AC4: the two pages together are exactly the unfoldered set, in the
    # filtered most-recent-first order — none skipped.
    walked = page1 + page2
    assert walked == unfoldered_ids, "page walk skipped/reordered unfoldered chats"
    assert set(walked) == set(unfoldered_ids)


# ---------------------------------------------------------------------------
# T-006: pinned exclusion composes with the folder filter. Covers R3 AC1 — a
# pinned, unfoldered chat does NOT appear in the recent list.
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_pinned_unfoldered_chat_excluded_from_recent(authenticated_user):
    """R3 AC1: pinning an unfoldered chat removes it from the recent list, and
    the folder filter still lets a plain unfoldered chat through.

    get_chat_title_id_list_by_user_id composes
    `or_(pinned.is_(False), pinned.is_(None))` (models/chats.py:391) on top of the
    folder_id=None filter — both must hold for a chat to appear.
    """
    plain_id = _create_chat(authenticated_user, "Plain Unfoldered")
    pinned_id = _create_chat(authenticated_user, "Pinned Unfoldered")

    # Pin the second chat (it stays unfoldered — folder_id is null).
    resp = authenticated_user.post(f"/api/v1/chats/{pinned_id}/pin")
    assert resp.status_code == 200, resp.text
    assert resp.json()["pinned"] is True

    ids = _recent_chat_ids(authenticated_user)
    assert plain_id in ids, "plain unfoldered chat missing from recent list"
    assert pinned_id not in ids, "pinned unfoldered chat leaked into recent list"


# ---------------------------------------------------------------------------
# T-007: archived exclusion composes with the folder filter. Covers R3 AC2 — an
# archived (unfoldered) chat does NOT appear under the default request.
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_archived_unfoldered_chat_excluded_from_recent(authenticated_user):
    """R3 AC2: archiving an unfoldered chat removes it from the default recent
    list (which is invoked with include_archived=False, routers/chats.py:34/41).

    get_chat_title_id_list_by_user_id applies `.filter_by(archived=False)`
    (models/chats.py:394) on top of folder_id=None when include_archived is False.
    """
    plain_id = _create_chat(authenticated_user, "Active Unfoldered")
    archived_id = _create_chat(authenticated_user, "Archived Unfoldered")

    # Archive the second chat (stays unfoldered).
    resp = authenticated_user.post(f"/api/v1/chats/{archived_id}/archive")
    assert resp.status_code == 200, resp.text
    assert resp.json()["archived"] is True

    ids = _recent_chat_ids(authenticated_user)
    assert plain_id in ids, "active unfoldered chat missing from recent list"
    assert archived_id not in ids, "archived chat leaked into recent list"


# ---------------------------------------------------------------------------
# T-008: ordering. Covers R3 AC3 — returned unfoldered chats are ordered
# most-recently-updated first.
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_recent_list_ordered_most_recently_updated_first(authenticated_user, test_user, db_session):
    """R3 AC3: the recent list is ordered by updated_at descending.

    Grounded in `order_by(Chat.updated_at.desc())` (models/chats.py:397). Chats
    are created out of timestamp order to prove the ordering comes from the query,
    not insertion order.
    """
    from tests.factories import ChatFactory

    uid = test_user["id"]
    base = 1_900_000_000
    # Insert deliberately out of order: mid, newest, oldest.
    mid = ChatFactory.create(db_session, user_id=uid, title="mid", folder_id=None, updated_at=base + 20)
    new = ChatFactory.create(db_session, user_id=uid, title="new", folder_id=None, updated_at=base + 30)
    old = ChatFactory.create(db_session, user_id=uid, title="old", folder_id=None, updated_at=base + 10)

    ids = _recent_chat_ids(authenticated_user)
    # Only our three chats exist for this user; expect strict desc-by-updated_at.
    assert ids == [new.id, mid.id, old.id], f"recent list not most-recently-updated first: {ids}"


# ---------------------------------------------------------------------------
# T-009: admin per-user chat-list endpoint. Covers R4 AC1 — GET /list/user/{id}
# returns the same unfoldered set per its EXISTING behavior: include-archived,
# and (unlike the recent list) it does NOT exclude pinned chats.
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_admin_per_user_list_unfoldered_include_archived_keeps_pinned(authenticated_admin, db_session):
    """R4 AC1: admin endpoint is backed by get_chat_list_by_user_id
    (routers/chats.py:86, include_archived=True). That method filters
    folder_id=None (models/chats.py:368) but does NOT filter pinned — so archived
    and pinned unfoldered chats appear, while foldered chats do not.
    """
    from tests.factories import ChatFactory, UserFactory

    target = UserFactory.create(db_session)
    base = 1_900_000_000

    active = ChatFactory.create(db_session, user_id=target.id, title="tgt-active", folder_id=None, updated_at=base + 4)
    archived = ChatFactory.create(
        db_session, user_id=target.id, title="tgt-archived", folder_id=None, archived=True, updated_at=base + 3
    )
    pinned = ChatFactory.create(
        db_session, user_id=target.id, title="tgt-pinned", folder_id=None, pinned=True, updated_at=base + 2
    )
    foldered = ChatFactory.create(
        db_session, user_id=target.id, title="tgt-foldered", folder_id="fa", updated_at=base + 1
    )

    resp = authenticated_admin.get(f"/api/v1/chats/list/user/{target.id}")
    assert resp.status_code == 200, resp.text
    ids = {c["id"] for c in resp.json()}

    # Unfoldered active, archived, AND pinned all appear (include_archived=True,
    # no pinned exclusion on this shared method).
    assert active.id in ids
    assert archived.id in ids, "admin per-user list dropped an archived unfoldered chat"
    assert pinned.id in ids, "admin per-user list wrongly excluded a pinned chat"
    # Foldered chat is excluded (folder_id=None filter).
    assert foldered.id not in ids, "admin per-user list leaked a foldered chat"
    assert ids == {active.id, archived.id, pinned.id}


# ---------------------------------------------------------------------------
# T-010: empty-search path. Covers R4 AC2 — GET /search?text= (empty) returns
# the same unfoldered set as before, routing through get_chat_list_by_user_id.
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_empty_search_returns_unfoldered_set(authenticated_user):
    """R4 AC2: an empty search text delegates to get_chat_list_by_user_id
    (models/chats.py:502 -> :360), so it returns unfoldered-only, matching the
    recent list for this (non-pinned, non-archived) dataset.

    Uses only unfoldered non-pinned non-archived chats + one foldered chat, so
    the recent-list pinned-exclusion difference does not confound the comparison.
    """
    u1 = _create_chat(authenticated_user, "Search Unfoldered 1")
    u2 = _create_chat(authenticated_user, "Search Unfoldered 2")
    u3 = _create_chat(authenticated_user, "Search Unfoldered 3")
    foldered = _create_chat(authenticated_user, "Search Foldered")
    folder_id = _create_folder(authenticated_user, "Search Folder")
    _assign_chat_to_folder(authenticated_user, foldered, folder_id)

    resp = authenticated_user.get("/api/v1/chats/search?text=")
    assert resp.status_code == 200, resp.text
    search_ids = {c["id"] for c in resp.json()}

    assert search_ids == {u1, u2, u3}, f"empty search not unfoldered-only: {search_ids}"
    assert foldered not in search_ids, "foldered chat leaked into empty-search results"
    # Same unfoldered membership as the recent-list default.
    assert search_ids == set(_recent_chat_ids(authenticated_user))
