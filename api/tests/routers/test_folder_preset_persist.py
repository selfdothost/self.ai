"""cavekit-chat-folders-rag-config.md R2 (Preset Fields Persist Through the
Existing Update Path) — T-003.

Scoped to the persistence half of R2: the three preset fields ride in on the
pre-existing ``POST /{id}/update`` path (no new endpoint), land in the folder's
existing ``meta`` JSON column (no migration), and a later read returns exactly
what was written. The omit-preserves / field-level merge nuance (R2 AC4) is
T-004's job (see ``test_folder_preset_merge.py``).

Note: as of T-007 the update path validates every preset reference against a
real, accessible record before persisting (cavekit R3). These persistence tests
therefore seed real records — owned by the writing user — for every id they
reference, so the write is accepted for the reason under test (persistence),
not rejected on a dangling reference. The rejection path itself is covered in
``test_folder_preset_validation.py``.
"""

import time

import pytest

from selfai_ui.models.folders import Folder, Folders
from selfai_ui.models.models import Model
from tests.factories import KnowledgeFactory, ToolFactory

# ---------------------------------------------------------------------------
# Record-seeding helpers — create real records owned by the writing user so
# their ids resolve through the R3 validation the update path now performs.
# ---------------------------------------------------------------------------


def _seed_model(db_session, user_id, model_id):
    now = int(time.time())
    db_session.add(
        Model(
            id=model_id,
            user_id=user_id,
            base_model_id=None,
            name=model_id,
            params={},
            meta={"description": "seed"},
            access_control=None,
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


@pytest.mark.tier0
def test_all_three_preset_fields_persist_and_read_back(authenticated_user, test_user, db_session):
    """R2 AC1 — submitting an update with a default model reference, tool
    references, and knowledge references stores all three, and a later read of
    the folder returns the same values."""
    uid = test_user["id"]
    _seed_model(db_session, uid, "qwen2.5-coder-32b")
    _seed_tool(db_session, uid, "web_search")
    _seed_tool(db_session, uid, "code_interpreter")
    _seed_knowledge(db_session, uid, "kb-alpha")
    _seed_knowledge(db_session, uid, "dataset-2026-curated")

    created = authenticated_user.post("/api/v1/folders/", json={"name": "Preset Folder"}).json()
    fid = created["id"]

    resp = authenticated_user.post(
        f"/api/v1/folders/{fid}/update",
        json={
            # unchanged name — a preset-only update re-submits the folder's own
            # name; this must not be rejected as a self name-collision.
            "name": "Preset Folder",
            "default_model_id": "qwen2.5-coder-32b",
            "tool_ids": ["web_search", "code_interpreter"],
            "knowledge_ids": ["kb-alpha", "dataset-2026-curated"],
        },
    )
    assert resp.status_code == 200

    refetch = authenticated_user.get(f"/api/v1/folders/{fid}").json()
    preset = refetch["meta"]["preset"]
    assert preset["default_model_id"] == "qwen2.5-coder-32b"
    assert preset["tool_ids"] == ["web_search", "code_interpreter"]
    assert preset["knowledge_ids"] == ["kb-alpha", "dataset-2026-curated"]


@pytest.mark.tier0
def test_preset_persists_through_existing_update_path_no_dedicated_endpoint(
    authenticated_user, test_app, test_user, db_session
):
    """R2 AC2 — the preset is persisted through the pre-existing folder-update
    operation, without introducing a separate dedicated preset-write operation.

    Verified two ways: (1) the shared ``/{id}/update`` route is the one that
    persists the preset, and (2) the app exposes no dedicated preset-write route
    under the folders prefix."""
    _seed_model(db_session, test_user["id"], "m-1")

    created = authenticated_user.post("/api/v1/folders/", json={"name": "Shared Path"}).json()
    fid = created["id"]

    # (1) The shared update path — not a preset-specific one — does the write.
    resp = authenticated_user.post(
        f"/api/v1/folders/{fid}/update",
        json={"name": "Shared Path", "default_model_id": "m-1"},
    )
    assert resp.status_code == 200
    refetch = authenticated_user.get(f"/api/v1/folders/{fid}").json()
    assert refetch["meta"]["preset"]["default_model_id"] == "m-1"

    # (2) No dedicated preset-write endpoint exists in the folders router.
    #
    # This app registers include_router() sub-apps as lazy `_IncludedRouter`
    # wrappers (not flattened APIRoute entries) — the mounted prefix lives on
    # `include_context.prefix`, and the sub-router's own routes (relative
    # paths) live on `original_router.routes`. A flat `route.path` scan over
    # `test_app.routes` never sees these at all.
    folders_router = next(
        (
            r
            for r in test_app.routes
            if type(r).__name__ == "_IncludedRouter"
            and getattr(r.include_context, "prefix", None) == "/api/v1/folders"
        ),
        None,
    )
    assert folders_router is not None, "expected the folders router to be registered under /api/v1/folders"

    folder_route_paths = [getattr(sub, "path", "") for sub in folders_router.original_router.routes]
    assert folder_route_paths, "expected folders routes to be registered"
    assert not any("preset" in p for p in folder_route_paths), (
        f"a dedicated preset endpoint must not exist; folder routes: {folder_route_paths}"
    )
    assert not any("preset" in p for p in folder_route_paths), (
        f"a dedicated preset endpoint must not exist; folder routes: {folder_route_paths}"
    )


@pytest.mark.tier0
def test_no_schema_migration_meta_column_preexists(authenticated_user, test_user, db_session):
    """R2 AC3 — persisting the preset requires no schema migration: the preset
    lands in the ``folder.meta`` JSON column that already exists on the folder
    table, not in any newly-added column."""
    # The persistence target column pre-exists on the folder model.
    column_names = {c.name for c in Folder.__table__.columns}
    assert "meta" in column_names

    uid = test_user["id"]
    _seed_model(db_session, uid, "m-x")
    _seed_tool(db_session, uid, "t-x")
    _seed_knowledge(db_session, uid, "k-x")

    created = authenticated_user.post("/api/v1/folders/", json={"name": "No Migration"}).json()
    fid = created["id"]
    authenticated_user.post(
        f"/api/v1/folders/{fid}/update",
        json={
            "name": "No Migration",
            "default_model_id": "m-x",
            "tool_ids": ["t-x"],
            "knowledge_ids": ["k-x"],
        },
    )

    # The preset was written into the existing meta column — inspect the raw row.
    row = db_session.query(Folder).filter_by(id=fid).first()
    assert row is not None
    assert row.meta["preset"]["default_model_id"] == "m-x"
    assert row.meta["preset"]["tool_ids"] == ["t-x"]
    assert row.meta["preset"]["knowledge_ids"] == ["k-x"]


@pytest.mark.tier0
def test_table_method_persists_preset_into_meta(authenticated_user, test_user):
    """R2 AC1/AC2 at the table layer — the update_folder_meta_by_id_and_user_id
    primitive shallow-merges into existing meta (top level) and round-trips the
    preset. Also confirms the meta write is user-scoped.

    This exercises the table primitive directly (below the router), so it does
    not pass through R3 reference validation and can use synthetic ids."""
    created = authenticated_user.post("/api/v1/folders/", json={"name": "Table Layer"}).json()
    fid = created["id"]
    uid = test_user["id"]

    preset = {"default_model_id": "mm", "tool_ids": ["tt"], "knowledge_ids": ["kk"]}
    updated = Folders.update_folder_meta_by_id_and_user_id(fid, uid, {"preset": preset})
    assert updated is not None
    assert updated.meta["preset"] == preset

    # A second, unrelated top-level meta key is merged in, not overwriting preset.
    Folders.update_folder_meta_by_id_and_user_id(fid, uid, {"unrelated": True})
    reread = Folders.get_folder_by_id_and_user_id(fid, uid)
    assert reread.meta["preset"] == preset
    assert reread.meta["unrelated"] is True

    # Wrong user cannot write this folder's meta.
    assert Folders.update_folder_meta_by_id_and_user_id(fid, "not-the-owner", {"x": 1}) is None
