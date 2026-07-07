"""
T-302 + T-303: Channels router CRUD + message operations.

Channels are admin-created. Users can list channels they're members of.
Messages can be posted by verified users, with reactions and threads.
"""

import pytest


@pytest.mark.tier0
def test_list_channels_empty_for_user(authenticated_user):
    resp = authenticated_user.get("/api/v1/channels/")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.tier0
def test_create_channel_user_forbidden(authenticated_user):
    """User-role cannot create channels."""
    resp = authenticated_user.post(
        "/api/v1/channels/create",
        json={"name": "user-channel", "description": "test"},
    )
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_create_channel_admin_succeeds(authenticated_admin):
    """Admin creates a channel."""
    resp = authenticated_admin.post(
        "/api/v1/channels/create",
        json={"name": "admin-channel", "description": "test"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "admin-channel"


@pytest.mark.tier0
def test_update_channel(authenticated_admin):
    created = authenticated_admin.post(
        "/api/v1/channels/create",
        json={"name": "original", "description": ""},
    ).json()
    resp = authenticated_admin.post(
        f"/api/v1/channels/{created['id']}/update",
        json={"name": "updated", "description": "new"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "updated"


@pytest.mark.tier0
def test_delete_channel(authenticated_admin):
    created = authenticated_admin.post(
        "/api/v1/channels/create",
        json={"name": "to-delete", "description": ""},
    ).json()
    resp = authenticated_admin.delete(
        f"/api/v1/channels/{created['id']}/delete"
    )
    assert resp.status_code == 200


@pytest.mark.tier0
def test_post_message_to_channel(authenticated_admin):
    """Admin posts a message to a channel."""
    channel = authenticated_admin.post(
        "/api/v1/channels/create",
        json={"name": "msg-channel", "description": ""},
    ).json()
    resp = authenticated_admin.post(
        f"/api/v1/channels/{channel['id']}/messages/post",
        json={"content": "Hello channel", "data": {}},
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "Hello channel"


@pytest.mark.tier0
def test_list_channel_messages(authenticated_admin):
    channel = authenticated_admin.post(
        "/api/v1/channels/create",
        json={"name": "list-msgs", "description": ""},
    ).json()
    authenticated_admin.post(
        f"/api/v1/channels/{channel['id']}/messages/post",
        json={"content": "msg1", "data": {}},
    )
    authenticated_admin.post(
        f"/api/v1/channels/{channel['id']}/messages/post",
        json={"content": "msg2", "data": {}},
    )
    resp = authenticated_admin.get(f"/api/v1/channels/{channel['id']}/messages")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) >= 2
    contents = [m["content"] for m in body]
    assert "msg1" in contents
    assert "msg2" in contents


# ---------------------------------------------------------------------------
# T-519: Reaction idempotency + caller-scoping + aggregated shape + remove-nonexistent
# Kit: cavekit-ui-router-tests-core-data.md R2 new-AC1..4 (kit AC14-17)
# ---------------------------------------------------------------------------

def _create_channel_with_two_members(authenticated_admin, authenticated_user):
    """Admin creates a public channel (no access_control → all users can read)."""
    return authenticated_admin.post(
        "/api/v1/channels/create",
        json={"name": "reactions-channel", "description": "", "access_control": None},
    ).json()


def _post_message(client, channel_id, content="hello"):
    return client.post(
        f"/api/v1/channels/{channel_id}/messages/post",
        json={"content": content, "data": {}},
    ).json()


def _get_reactions(client, channel_id, message_id):
    """Read message and return its reactions list."""
    resp = client.get(
        f"/api/v1/channels/{channel_id}/messages/{message_id}"
    )
    return resp.json().get("reactions", [])


@pytest.mark.tier1
@pytest.mark.xfail(
    reason="ROUTER BUG: add_reaction_to_message inserts a new row every call — no "
    "dedup on (user_id, message_id, name). Violates R2 new-AC1 (idempotency). See "
    "selfai_ui/models/messages.py:211-227.",
    strict=False,
)
def test_reaction_idempotency_same_user_same_name(authenticated_admin):
    """R2 new-AC1: adding same reaction twice by same user = one row, 2xx no-op."""
    ch = authenticated_admin.post(
        "/api/v1/channels/create",
        json={"name": "r-idem", "description": ""},
    ).json()
    msg = _post_message(authenticated_admin, ch["id"])
    # First add
    r1 = authenticated_admin.post(
        f"/api/v1/channels/{ch['id']}/messages/{msg['id']}/reactions/add",
        json={"name": "👍"},
    )
    assert r1.status_code == 200
    # Duplicate add
    r2 = authenticated_admin.post(
        f"/api/v1/channels/{ch['id']}/messages/{msg['id']}/reactions/add",
        json={"name": "👍"},
    )
    assert r2.status_code == 200
    # Aggregated reactions should show one entry with count=1
    reactions = _get_reactions(authenticated_admin, ch["id"], msg["id"])
    thumbs = [r for r in reactions if r["name"] == "👍"]
    assert len(thumbs) == 1
    assert thumbs[0]["count"] == 1, (
        f"Expected count=1 after duplicate add (idempotent), got {thumbs[0]}"
    )


@pytest.mark.tier1
def test_reaction_caller_scoped_removal(
    client, test_admin, test_user
):
    """R2 new-AC2: removing caller's reaction leaves others' intact.

    NOTE: admin and user share the same underlying client; we swap bearer
    tokens before each call so each call authenticates as the intended user.
    Use plain  + manual bearer swap to avoid fixture-ordering races.
    """
    # Admin creates channel + message
    client.headers['Authorization'] = f"Bearer {test_admin['token']}"
    ch = client.post(
        "/api/v1/channels/create",
        json={"name": "r-scope", "description": ""},
    ).json()
    msg = _post_message(client, ch["id"])

    # User A (admin) reacts 👍
    client.headers["Authorization"] = f"Bearer {test_admin['token']}"
    r1 = client.post(
        f"/api/v1/channels/{ch['id']}/messages/{msg['id']}/reactions/add",
        json={"name": "👍"},
    )
    assert r1.status_code == 200
    # User B reacts 👍
    client.headers["Authorization"] = f"Bearer {test_user['token']}"
    r2 = client.post(
        f"/api/v1/channels/{ch['id']}/messages/{msg['id']}/reactions/add",
        json={"name": "👍"},
    )
    assert r2.status_code == 200

    # User A removes their 👍
    client.headers["Authorization"] = f"Bearer {test_admin['token']}"
    rm = client.post(
        f"/api/v1/channels/{ch['id']}/messages/{msg['id']}/reactions/remove",
        json={"name": "👍"},
    )
    assert rm.status_code == 200

    # Aggregated should show only user B's 👍
    reactions = _get_reactions(client, ch["id"], msg["id"])
    thumbs = [r for r in reactions if r["name"] == "👍"]
    assert len(thumbs) == 1
    assert thumbs[0]["count"] == 1
    assert thumbs[0]["user_ids"] == [test_user["id"]], (
        f"Expected only user B ({test_user['id']}) remaining, "
        f"got {thumbs[0]['user_ids']}"
    )


@pytest.mark.tier1
def test_reaction_aggregated_shape(
    authenticated_admin, test_admin, test_user
):
    """R2 new-AC3: aggregated response has one entry per name with user_ids + count.

    Skips the duplicate-same-user case (covered by T-519 idempotency xfail).
    Uses two distinct users + two distinct reactions to validate aggregation.
    """
    ch = authenticated_admin.post(
        "/api/v1/channels/create",
        json={"name": "r-shape", "description": ""},
    ).json()
    msg = _post_message(authenticated_admin, ch["id"])
    client = authenticated_admin

    # Admin reacts with 👍 and ❤️
    client.headers["Authorization"] = f"Bearer {test_admin['token']}"
    client.post(
        f"/api/v1/channels/{ch['id']}/messages/{msg['id']}/reactions/add",
        json={"name": "👍"},
    )
    client.post(
        f"/api/v1/channels/{ch['id']}/messages/{msg['id']}/reactions/add",
        json={"name": "❤️"},
    )
    # User reacts with 👍 only
    client.headers["Authorization"] = f"Bearer {test_user['token']}"
    client.post(
        f"/api/v1/channels/{ch['id']}/messages/{msg['id']}/reactions/add",
        json={"name": "👍"},
    )

    reactions = _get_reactions(client, ch["id"], msg["id"])
    by_name = {r["name"]: r for r in reactions}
    assert "👍" in by_name
    assert "❤️" in by_name
    thumbs = by_name["👍"]
    # Two distinct users reacted 👍
    assert thumbs["count"] == len(thumbs["user_ids"])
    assert set(thumbs["user_ids"]) == {test_admin["id"], test_user["id"]}
    # No duplicate user ids within a single reaction name
    assert len(thumbs["user_ids"]) == len(set(thumbs["user_ids"]))
    heart = by_name["❤️"]
    assert heart["user_ids"] == [test_admin["id"]]
    assert heart["count"] == 1


@pytest.mark.tier1
def test_reaction_remove_nonexistent_is_noop(authenticated_admin):
    """R2 new-AC4: removing a reaction never added is 2xx no-op."""
    ch = authenticated_admin.post(
        "/api/v1/channels/create",
        json={"name": "r-nonexistent", "description": ""},
    ).json()
    msg = _post_message(authenticated_admin, ch["id"])
    resp = authenticated_admin.post(
        f"/api/v1/channels/{ch['id']}/messages/{msg['id']}/reactions/remove",
        json={"name": "🎉"},
    )
    assert resp.status_code == 200
    # Store unchanged — no reactions on the message
    reactions = _get_reactions(authenticated_admin, ch["id"], msg["id"])
    assert reactions == []


# ---------------------------------------------------------------------------
# T-520: Flat threads + grandchild rejection + reply listing + cascade delete
# Kit: cavekit-ui-router-tests-core-data.md R2 new-AC5..9 (kit AC18-22)
# ---------------------------------------------------------------------------


def _post_reply(client, channel_id, parent_id, content="reply"):
    return client.post(
        f"/api/v1/channels/{channel_id}/messages/post",
        json={"content": content, "data": {}, "parent_id": parent_id},
    )


@pytest.mark.tier1
def test_reply_parent_id_points_to_root(authenticated_admin):
    """R2 new-AC5: a reply has non-null parent_id; the root has parent_id=null."""
    ch = authenticated_admin.post(
        "/api/v1/channels/create",
        json={"name": "t-root", "description": ""},
    ).json()
    root = _post_message(authenticated_admin, ch["id"], "root-msg")
    assert root.get("parent_id") in (None, ""), (
        f"Root message should have null parent_id, got {root.get('parent_id')!r}"
    )
    reply = _post_reply(authenticated_admin, ch["id"], root["id"]).json()
    assert reply["parent_id"] == root["id"], (
        f"Reply parent_id should be {root['id']}, got {reply['parent_id']!r}"
    )


@pytest.mark.tier1
@pytest.mark.xfail(
    reason="ROUTER BUG: channels router does not enforce flat threads — "
    "replying to a reply succeeds, creating a grandchild. Violates R2 new-AC6. "
    "See selfai_ui/routers/channels.py post_new_message (no parent_id validation).",
    strict=False,
)
def test_grandchild_reply_rejected(authenticated_admin):
    """R2 new-AC6: reply-to-a-reply returns 4xx and does not persist."""
    ch = authenticated_admin.post(
        "/api/v1/channels/create",
        json={"name": "t-grandchild", "description": ""},
    ).json()
    root = _post_message(authenticated_admin, ch["id"], "root-msg")
    reply = _post_reply(authenticated_admin, ch["id"], root["id"], "reply-1").json()
    # Attempt to reply to the reply (grandchild)
    resp = _post_reply(authenticated_admin, ch["id"], reply["id"], "grandchild")
    assert 400 <= resp.status_code < 500, (
        f"Expected 4xx for grandchild reply, got {resp.status_code}: {resp.text[:200]}"
    )
    # Verify no grandchild row — listing replies for `reply` must be empty
    thread = authenticated_admin.get(
        f"/api/v1/channels/{ch['id']}/messages/{reply['id']}/thread"
    ).json()
    # Router includes parent at end of thread list if under limit — so
    # accept either [] or [reply] but never include a grandchild
    grandchildren = [m for m in thread if m.get("parent_id") == reply["id"]]
    assert len(grandchildren) == 0, (
        f"Expected no grandchildren, got {len(grandchildren)}: {grandchildren}"
    )


@pytest.mark.tier1
@pytest.mark.xfail(
    reason="ROUTER BUG: get_messages_by_parent_id appends the parent message to "
    "the thread list when under limit. Violates R2 new-AC7 (listing excludes parent). "
    "See selfai_ui/models/messages.py:174-196.",
    strict=False,
)
def test_reply_listing_children_only_chronological(authenticated_admin):
    """R2 new-AC7: thread listing returns children only, chronological order."""
    ch = authenticated_admin.post(
        "/api/v1/channels/create",
        json={"name": "t-listing", "description": ""},
    ).json()
    root = _post_message(authenticated_admin, ch["id"], "root-msg")
    import time as _t
    r1 = _post_reply(authenticated_admin, ch["id"], root["id"], "r1").json()
    _t.sleep(0.001)
    r2 = _post_reply(authenticated_admin, ch["id"], root["id"], "r2").json()
    _t.sleep(0.001)
    r3 = _post_reply(authenticated_admin, ch["id"], root["id"], "r3").json()

    thread = authenticated_admin.get(
        f"/api/v1/channels/{ch['id']}/messages/{root['id']}/thread"
    ).json()
    # AC: exclude parent, include children only
    ids_in_thread = [m["id"] for m in thread]
    assert root["id"] not in ids_in_thread, (
        f"Thread listing must exclude the parent (root) message; got {ids_in_thread}"
    )
    assert set(ids_in_thread) == {r1["id"], r2["id"], r3["id"]}


@pytest.mark.tier1
@pytest.mark.xfail(
    reason="ROUTER BUG: delete_message_by_id deletes only the single message + its "
    "reactions; it does NOT cascade to replies (delete_replies_by_id exists but is "
    "not called). Violates R2 new-AC8. See selfai_ui/models/messages.py:268-276.",
    strict=False,
)
def test_cascade_delete_parent_removes_replies(authenticated_admin):
    """R2 new-AC8: deleting parent cascades to replies."""
    ch = authenticated_admin.post(
        "/api/v1/channels/create",
        json={"name": "t-cascade", "description": ""},
    ).json()
    root = _post_message(authenticated_admin, ch["id"], "root")
    r1 = _post_reply(authenticated_admin, ch["id"], root["id"], "r1").json()
    r2 = _post_reply(authenticated_admin, ch["id"], root["id"], "r2").json()

    # Delete the parent
    del_resp = authenticated_admin.delete(
        f"/api/v1/channels/{ch['id']}/messages/{root['id']}/delete"
    )
    assert del_resp.status_code == 200

    # Each reply should now be 404 on direct read
    for reply in (r1, r2):
        resp = authenticated_admin.get(
            f"/api/v1/channels/{ch['id']}/messages/{reply['id']}"
        )
        assert resp.status_code == 404, (
            f"Expected 404 for deleted reply {reply['id']}, got {resp.status_code}"
        )


@pytest.mark.tier1
def test_single_reply_delete_leaves_parent_and_siblings(authenticated_admin):
    """R2 new-AC9: deleting a single reply removes only that reply."""
    ch = authenticated_admin.post(
        "/api/v1/channels/create",
        json={"name": "t-sibling", "description": ""},
    ).json()
    root = _post_message(authenticated_admin, ch["id"], "root")
    r1 = _post_reply(authenticated_admin, ch["id"], root["id"], "r1").json()
    r2 = _post_reply(authenticated_admin, ch["id"], root["id"], "r2").json()
    r3 = _post_reply(authenticated_admin, ch["id"], root["id"], "r3").json()

    # Delete only r2
    del_resp = authenticated_admin.delete(
        f"/api/v1/channels/{ch['id']}/messages/{r2['id']}/delete"
    )
    assert del_resp.status_code == 200

    # r2 is gone
    assert authenticated_admin.get(
        f"/api/v1/channels/{ch['id']}/messages/{r2['id']}"
    ).status_code == 404

    # Parent and siblings remain readable
    for surviving in (root, r1, r3):
        resp = authenticated_admin.get(
            f"/api/v1/channels/{ch['id']}/messages/{surviving['id']}"
        )
        assert resp.status_code == 200, (
            f"Expected 200 for surviving {surviving['id']}, got {resp.status_code}"
        )
