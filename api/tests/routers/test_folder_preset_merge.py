"""cavekit-chat-folders-rag-config.md R2 AC4 (merge-on-omit) — T-004.

R2 AC4: a folder update that OMITS the preset fields must PRESERVE the
previously stored preset unchanged (merge semantics, not replace) — an
unrelated update (e.g. a rename) never clears or corrupts an existing preset.

This is verified at two layers:
  1. Router — a rename-only ``POST /{id}/update`` (carrying none of the three
     preset fields) leaves the stored ``meta["preset"]`` intact, and provably
     never invokes the meta-write path at all (the ``PRESET_FIELDS`` presence
     gate short-circuits it).
  2. Table primitive — ``update_folder_meta_by_id_and_user_id`` shallow-merges
     at the top level, so a write that omits the ``preset`` key never disturbs
     a stored preset.
"""

import time
from unittest.mock import patch

import pytest

from selfai_ui.models.folders import Folders
from selfai_ui.models.models import Model


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


@pytest.mark.tier0
def test_rename_only_update_preserves_stored_preset(authenticated_user, test_user, db_session):
    """R2 AC4 — after a preset is stored, a rename-only update (no preset
    fields) leaves the stored preset byte-for-byte unchanged."""
    uid = test_user["id"]
    _seed_model(db_session, uid, "m-keep")

    created = authenticated_user.post("/api/v1/folders/", json={"name": "Has Preset"}).json()
    fid = created["id"]

    # Write a preset first.
    resp = authenticated_user.post(
        f"/api/v1/folders/{fid}/update",
        json={"name": "Has Preset", "default_model_id": "m-keep"},
    )
    assert resp.status_code == 200
    stored = authenticated_user.get(f"/api/v1/folders/{fid}").json()["meta"]["preset"]

    # Rename only — no preset fields in the payload at all.
    resp = authenticated_user.post(f"/api/v1/folders/{fid}/update", json={"name": "Renamed"})
    assert resp.status_code == 200

    refetch = authenticated_user.get(f"/api/v1/folders/{fid}").json()
    assert refetch["name"] == "Renamed"
    # Preset survives the rename unchanged (merge, not replace, not clear).
    assert refetch["meta"]["preset"] == stored
    assert refetch["meta"]["preset"]["default_model_id"] == "m-keep"


@pytest.mark.tier0
def test_rename_only_update_never_invokes_meta_write(authenticated_user):
    """R2 AC4 mechanism — a rename-only update must not even call the
    preset/meta write path. Spy on the table primitive and assert it is never
    invoked when the payload carries none of the three preset fields."""
    created = authenticated_user.post("/api/v1/folders/", json={"name": "Plain"}).json()
    fid = created["id"]

    with patch.object(
        Folders,
        "update_folder_meta_by_id_and_user_id",
        wraps=Folders.update_folder_meta_by_id_and_user_id,
    ) as spy:
        resp = authenticated_user.post(f"/api/v1/folders/{fid}/update", json={"name": "Plain Renamed"})

    assert resp.status_code == 200
    spy.assert_not_called()


@pytest.mark.tier0
def test_table_merge_preserves_preset_when_preset_key_omitted(authenticated_user, test_user):
    """R2 AC4 at the table layer — writing an UNRELATED top-level meta key via
    the shallow-merge primitive never disturbs a previously stored preset. This
    is the mechanism the router relies on to guarantee merge-on-omit."""
    created = authenticated_user.post("/api/v1/folders/", json={"name": "Table Merge"}).json()
    fid = created["id"]
    uid = test_user["id"]

    preset = {"default_model_id": "mm", "tool_ids": ["tt"], "knowledge_ids": ["kk"]}
    Folders.update_folder_meta_by_id_and_user_id(fid, uid, {"preset": preset})

    # A later meta write that OMITS the preset key must not clear it.
    Folders.update_folder_meta_by_id_and_user_id(fid, uid, {"something_else": 42})

    reread = Folders.get_folder_by_id_and_user_id(fid, uid)
    assert reread.meta["preset"] == preset
    assert reread.meta["something_else"] == 42
