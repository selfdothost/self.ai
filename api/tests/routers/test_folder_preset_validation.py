"""cavekit-chat-folders-rag-config.md R3 (Referenced ID Validation on Write) —
T-007.

The update path validates every reference a preset carries — default model,
each tool, each knowledge entry — against a real record accessible to the
writer, BEFORE any database write. A write carrying any unresolved reference is
rejected atomically (HTTP 400) with a generic detail; nothing persists, and the
error never distinguishes a nonexistent record from a merely-inaccessible one.

  AC1 — bad default-model ref rejected.
  AC2 — any bad tool ref rejected.
  AC3 — any bad knowledge ref rejected.
  AC4 — a rejected write persists nothing (not the preset, not a concurrent rename).
  AC5 — a write whose every ref resolves is accepted and persisted.

Plus the R3 no-existence-leak property: an existing-but-inaccessible ref is
rejected identically (same status, same detail) to a nonexistent one.
"""

import time

import pytest

from selfai_ui.models.folders import Folder
from selfai_ui.models.models import Model
from tests.factories import KnowledgeFactory, ToolFactory, UserFactory


def _seed_model(db_session, user_id, model_id, access_control=None):
    now = int(time.time())
    db_session.add(
        Model(
            id=model_id,
            user_id=user_id,
            base_model_id=None,
            name=model_id,
            params={},
            meta={"description": "seed"},
            access_control=access_control,
            is_active=True,
            updated_at=now,
            created_at=now,
        )
    )
    db_session.commit()


def _seed_tool(db_session, user_id, tool_id):
    ToolFactory.create(db_session, id=tool_id, user_id=user_id)


def _seed_knowledge(db_session, user_id, knowledge_id):
    KnowledgeFactory.create(db_session, id=knowledge_id, user_id=user_id)


def _make_folder(authenticated_user, name="Preset Target"):
    return authenticated_user.post("/api/v1/folders/", json={"name": name}).json()["id"]


# ---------------------------------------------------------------------------
# AC1 — unresolved default-model reference rejected
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_unresolved_model_ref_rejected(authenticated_user, test_user, db_session):
    fid = _make_folder(authenticated_user)
    resp = authenticated_user.post(
        f"/api/v1/folders/{fid}/update",
        json={"name": "Preset Target", "default_model_id": "no-such-model"},
    )
    assert resp.status_code == 400
    # Nothing persisted.
    row = db_session.query(Folder).filter_by(id=fid).first()
    assert (row.meta or {}).get("preset") is None


# ---------------------------------------------------------------------------
# AC2 — any unresolved tool reference rejected
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_unresolved_tool_ref_rejected(authenticated_user, test_user, db_session):
    uid = test_user["id"]
    _seed_model(db_session, uid, "ok-model")
    _seed_tool(db_session, uid, "ok-tool")

    fid = _make_folder(authenticated_user)
    # One good tool, one that does not resolve → whole write rejected.
    resp = authenticated_user.post(
        f"/api/v1/folders/{fid}/update",
        json={
            "name": "Preset Target",
            "default_model_id": "ok-model",
            "tool_ids": ["ok-tool", "ghost-tool"],
        },
    )
    assert resp.status_code == 400
    row = db_session.query(Folder).filter_by(id=fid).first()
    assert (row.meta or {}).get("preset") is None


# ---------------------------------------------------------------------------
# AC3 — any unresolved knowledge reference rejected
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_unresolved_knowledge_ref_rejected(authenticated_user, test_user, db_session):
    uid = test_user["id"]
    _seed_knowledge(db_session, uid, "ok-kb")

    fid = _make_folder(authenticated_user)
    resp = authenticated_user.post(
        f"/api/v1/folders/{fid}/update",
        json={
            "name": "Preset Target",
            "knowledge_ids": ["ok-kb", "ghost-kb"],
        },
    )
    assert resp.status_code == 400
    row = db_session.query(Folder).filter_by(id=fid).first()
    assert (row.meta or {}).get("preset") is None


# ---------------------------------------------------------------------------
# AC4 — a rejected write persists nothing (atomic — validated before any write)
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_rejected_write_persists_neither_preset_nor_rename(authenticated_user, test_user, db_session):
    """AC4 — a request that carries BOTH a rename and an invalid preset must
    persist neither: validation runs before any database write, so the folder's
    name and its prior preset are both left untouched."""
    uid = test_user["id"]
    _seed_model(db_session, uid, "good-model")

    fid = _make_folder(authenticated_user, name="Original")
    # Establish a valid stored preset first.
    ok = authenticated_user.post(
        f"/api/v1/folders/{fid}/update",
        json={"name": "Original", "default_model_id": "good-model"},
    )
    assert ok.status_code == 200

    # Now a rename + an unresolved model ref in one request.
    resp = authenticated_user.post(
        f"/api/v1/folders/{fid}/update",
        json={"name": "Renamed", "default_model_id": "ghost-model"},
    )
    assert resp.status_code == 400

    refetch = authenticated_user.get(f"/api/v1/folders/{fid}").json()
    # Rename did NOT persist — atomicity across the whole update.
    assert refetch["name"] == "Original"
    # Prior preset intact; the rejected preset was never written.
    assert refetch["meta"]["preset"]["default_model_id"] == "good-model"


# ---------------------------------------------------------------------------
# AC5 — a fully-resolvable write is accepted and persisted
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_fully_resolvable_preset_accepted_and_persisted(authenticated_user, test_user, db_session):
    uid = test_user["id"]
    _seed_model(db_session, uid, "m-good")
    _seed_tool(db_session, uid, "t-good")
    _seed_knowledge(db_session, uid, "k-good")

    fid = _make_folder(authenticated_user)
    resp = authenticated_user.post(
        f"/api/v1/folders/{fid}/update",
        json={
            "name": "Preset Target",
            "default_model_id": "m-good",
            "tool_ids": ["t-good"],
            "knowledge_ids": ["k-good"],
        },
    )
    assert resp.status_code == 200

    preset = authenticated_user.get(f"/api/v1/folders/{fid}").json()["meta"]["preset"]
    assert preset["default_model_id"] == "m-good"
    assert preset["tool_ids"] == ["t-good"]
    assert preset["knowledge_ids"] == ["k-good"]


# ---------------------------------------------------------------------------
# Builtin "web_search" tool_id — resolved by the live ENABLE_RAG_WEB_SEARCH
# config flag, not the tool table (no tool row for it exists or ever will)
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_web_search_tool_id_accepted_when_enabled(authenticated_user, test_app):
    original = test_app.state.config.ENABLE_RAG_WEB_SEARCH
    test_app.state.config.ENABLE_RAG_WEB_SEARCH = True
    try:
        fid = _make_folder(authenticated_user)
        resp = authenticated_user.post(
            f"/api/v1/folders/{fid}/update",
            json={"name": "Preset Target", "tool_ids": ["web_search"]},
        )
        assert resp.status_code == 200
        preset = authenticated_user.get(f"/api/v1/folders/{fid}").json()["meta"]["preset"]
        assert preset["tool_ids"] == ["web_search"]
    finally:
        test_app.state.config.ENABLE_RAG_WEB_SEARCH = original


@pytest.mark.tier0
def test_web_search_tool_id_rejected_when_disabled(authenticated_user, test_app, db_session):
    original = test_app.state.config.ENABLE_RAG_WEB_SEARCH
    test_app.state.config.ENABLE_RAG_WEB_SEARCH = False
    try:
        fid = _make_folder(authenticated_user)
        resp = authenticated_user.post(
            f"/api/v1/folders/{fid}/update",
            json={"name": "Preset Target", "tool_ids": ["web_search"]},
        )
        assert resp.status_code == 400
        row = db_session.query(Folder).filter_by(id=fid).first()
        assert (row.meta or {}).get("preset") is None
    finally:
        test_app.state.config.ENABLE_RAG_WEB_SEARCH = original


# ---------------------------------------------------------------------------
# R3 no-existence-leak — inaccessible rejected identically to nonexistent
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_inaccessible_ref_rejected_identically_to_nonexistent(authenticated_user, test_user, db_session):
    """R3 — a reference to a record that EXISTS but is outside the writer's
    access scope is rejected with the SAME status and SAME generic detail as a
    reference that does not exist at all. The response leaks nothing that would
    let a caller tell the two apart."""
    # A private model owned by a different user: exists, but inaccessible.
    other = UserFactory.create(db_session)
    _seed_model(db_session, other.id, "private-model", access_control={})

    fid_a = _make_folder(authenticated_user, name="Folder A")
    resp_inaccessible = authenticated_user.post(
        f"/api/v1/folders/{fid_a}/update",
        json={"name": "Folder A", "default_model_id": "private-model"},
    )

    fid_b = _make_folder(authenticated_user, name="Folder B")
    resp_nonexistent = authenticated_user.post(
        f"/api/v1/folders/{fid_b}/update",
        json={"name": "Folder B", "default_model_id": "utterly-nonexistent"},
    )

    assert resp_inaccessible.status_code == resp_nonexistent.status_code == 400
    # Byte-identical detail — no existence signal leaks.
    assert resp_inaccessible.json()["detail"] == resp_nonexistent.json()["detail"]
    # Neither write persisted a preset.
    for fid in (fid_a, fid_b):
        row = db_session.query(Folder).filter_by(id=fid).first()
        assert (row.meta or {}).get("preset") is None
