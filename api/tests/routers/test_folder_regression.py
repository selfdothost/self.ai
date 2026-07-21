"""cavekit-chat-folders-rag-config.md R4 (No Regression to Existing Folder
Behavior) — T-012, T-013, T-014.

R4 asserts that adding preset configuration to folders changed NO pre-existing
folder behavior. Every folder operation that existed before this feature must
still behave exactly as before. These are pure regression tests over the
folders router; they exercise no preset field at all except where a test
deliberately proves a preset was NOT implied or created.

Layout, one class per task:

  * ``TestCreateReparentExpandRegression`` (T-012) — create (incl. presetless),
    reparent (move), expand/collapse.
  * ``TestRenameDeleteRegression`` (T-013) — rename and recursive subtree
    delete, WITH the one documented, intentional exception: the rename
    self-collision fix from T-003. A rename to a genuinely different,
    already-taken name still 400s (the real collision check is intact); a
    rename to the folder's own current name now succeeds/no-ops (the fixed
    latent bug — the original code compared a folder against itself). Both
    branches are asserted here so the fix is never mistaken for a regression.
  * ``TestLegacyPresetlessFolderRegression`` (T-014) — a folder that predates
    this feature and carries no preset (``meta`` is NULL) remains fully usable:
    reading it back returns no preset (not an error, not a spurious default),
    and it can still be renamed, reparented, and deleted normally.

Test-DB note: the folders router's real ``get_db`` opens a session bound to the
same file-backed SQLite database the ``db_session`` fixture writes to (conftest
pins ``DATABASE_URL`` before importing ``selfai_ui``), and both sides commit, so
rows inserted directly via ``db_session`` are visible to the router and vice
versa. That is what lets T-014 seed a legacy row directly and then drive it
through the HTTP router.
"""

import time
import uuid

import pytest

from selfai_ui.models.folders import Folder

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create(authenticated_user, name):
    """Create a root folder via the router; return its full JSON body."""
    resp = authenticated_user.post("/api/v1/folders/", json={"name": name})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _seed_legacy_folder(db_session, user_id, name, parent_id=None):
    """Insert a folder row DIRECTLY, exactly as one would have existed before
    this feature: ``meta`` is NULL (no preset key, no preset structure at all).

    Returns the folder id.
    """
    fid = str(uuid.uuid4())
    now = int(time.time())
    db_session.add(
        Folder(
            id=fid,
            parent_id=parent_id,
            user_id=user_id,
            name=name,
            items=None,
            meta=None,  # the whole point: a pre-feature folder carries no meta
            is_expanded=False,
            created_at=now,
            updated_at=now,
        )
    )
    db_session.commit()
    return fid


# ===========================================================================
# T-012 — create (incl. presetless), reparent, expand/collapse
# ===========================================================================


class TestCreateReparentExpandRegression:
    """R4 AC1 (create, incl. no preset), AC3 (reparent), AC4 (expand/collapse)."""

    # -- create ------------------------------------------------------------

    @pytest.mark.tier0
    def test_create_folder_returns_presetless_folder(self, authenticated_user):
        """R4 AC1 — creating a folder behaves exactly as before: a root folder
        with no parent, collapsed by default, and NO preset implied (``meta`` is
        null). The create path never touched a preset before this feature and
        still does not."""
        body = _create(authenticated_user, "Plain New Folder")

        assert body["name"] == "Plain New Folder"
        assert body["parent_id"] is None
        assert body["is_expanded"] is False
        # No preset implied at creation — meta is null, not a spurious default.
        assert body["meta"] is None

        # And a read-back agrees.
        refetch = authenticated_user.get(f"/api/v1/folders/{body['id']}").json()
        assert refetch["meta"] is None

    @pytest.mark.tier0
    def test_create_duplicate_root_name_rejected(self, authenticated_user):
        """R4 AC1 — the pre-existing duplicate-name guard at the root is intact:
        creating a second folder with the same name under the same parent 400s,
        exactly as before."""
        _create(authenticated_user, "Dup")
        resp = authenticated_user.post("/api/v1/folders/", json={"name": "Dup"})
        assert resp.status_code == 400

    @pytest.mark.tier0
    def test_create_ignores_preset_fields_in_payload(self, authenticated_user):
        """R4 AC1 — the create endpoint accepts only a name and never persists a
        preset, even if preset-shaped fields are smuggled into the create
        payload. Pre-feature, create ignored any extra fields; that is unchanged
        (preset writes go exclusively through ``/{id}/update``, cavekit R2)."""
        resp = authenticated_user.post(
            "/api/v1/folders/",
            json={
                "name": "Create With Extras",
                "default_model_id": "should-be-ignored",
                "tool_ids": ["nope"],
                "knowledge_ids": ["nope"],
            },
        )
        assert resp.status_code == 200
        # The extras did NOT become a stored preset — create never writes meta.
        refetch = authenticated_user.get(f"/api/v1/folders/{resp.json()['id']}").json()
        assert refetch["meta"] is None

    # -- reparent (move) ---------------------------------------------------

    @pytest.mark.tier0
    def test_reparent_moves_folder_under_new_parent(self, authenticated_user):
        """R4 AC3 — moving a folder sets its parent_id, exactly as before."""
        parent = _create(authenticated_user, "Parent")
        child = _create(authenticated_user, "Child")

        resp = authenticated_user.post(
            f"/api/v1/folders/{child['id']}/update/parent",
            json={"parent_id": parent["id"]},
        )
        assert resp.status_code == 200
        assert resp.json()["parent_id"] == parent["id"]

        refetch = authenticated_user.get(f"/api/v1/folders/{child['id']}").json()
        assert refetch["parent_id"] == parent["id"]

    @pytest.mark.tier0
    def test_reparent_to_root_clears_parent(self, authenticated_user):
        """R4 AC3 — moving a folder back to the root (parent_id null) works,
        unchanged."""
        parent = _create(authenticated_user, "P")
        child = _create(authenticated_user, "C")
        authenticated_user.post(
            f"/api/v1/folders/{child['id']}/update/parent",
            json={"parent_id": parent["id"]},
        )

        resp = authenticated_user.post(
            f"/api/v1/folders/{child['id']}/update/parent",
            json={"parent_id": None},
        )
        assert resp.status_code == 200
        assert resp.json()["parent_id"] is None

    @pytest.mark.tier0
    def test_reparent_name_collision_under_target_rejected(self, authenticated_user):
        """R4 AC3 — the pre-existing collision guard on move is intact: moving a
        folder under a parent that already has a same-named child 400s."""
        parent = _create(authenticated_user, "Target")
        # A child already named "Twin" living under the target parent.
        first = _create(authenticated_user, "Twin")
        authenticated_user.post(
            f"/api/v1/folders/{first['id']}/update/parent",
            json={"parent_id": parent["id"]},
        )
        # A second root folder, also "Twin" (allowed at root since the first
        # one has moved away), now tries to move under the same parent.
        second = _create(authenticated_user, "Twin")
        resp = authenticated_user.post(
            f"/api/v1/folders/{second['id']}/update/parent",
            json={"parent_id": parent["id"]},
        )
        assert resp.status_code == 400

    @pytest.mark.tier0
    def test_reparent_missing_folder_404(self, authenticated_user):
        """R4 AC3 — reparenting a nonexistent folder 404s, unchanged."""
        resp = authenticated_user.post(
            "/api/v1/folders/does-not-exist/update/parent",
            json={"parent_id": None},
        )
        assert resp.status_code == 404

    # -- expand / collapse -------------------------------------------------

    @pytest.mark.tier0
    def test_expand_then_collapse_roundtrip(self, authenticated_user):
        """R4 AC4 — expanding and collapsing a folder flips is_expanded and
        persists, exactly as before."""
        folder = _create(authenticated_user, "Expandable")
        assert folder["is_expanded"] is False  # collapsed on creation

        expanded = authenticated_user.post(
            f"/api/v1/folders/{folder['id']}/update/expanded",
            json={"is_expanded": True},
        )
        assert expanded.status_code == 200
        assert expanded.json()["is_expanded"] is True
        assert authenticated_user.get(f"/api/v1/folders/{folder['id']}").json()["is_expanded"] is True

        collapsed = authenticated_user.post(
            f"/api/v1/folders/{folder['id']}/update/expanded",
            json={"is_expanded": False},
        )
        assert collapsed.status_code == 200
        assert collapsed.json()["is_expanded"] is False
        assert authenticated_user.get(f"/api/v1/folders/{folder['id']}").json()["is_expanded"] is False

    @pytest.mark.tier0
    def test_expand_missing_folder_404(self, authenticated_user):
        """R4 AC4 — toggling expansion on a nonexistent folder 404s, unchanged."""
        resp = authenticated_user.post(
            "/api/v1/folders/does-not-exist/update/expanded",
            json={"is_expanded": True},
        )
        assert resp.status_code == 404


# ===========================================================================
# T-013 — rename + recursive subtree delete
# (with the one documented, intentional same-name no-op exception)
# ===========================================================================


class TestRenameDeleteRegression:
    """R4 AC2 (rename) and R4 AC5 (recursive delete)."""

    # -- rename: unchanged behavior ---------------------------------------

    @pytest.mark.tier0
    def test_rename_to_fresh_name_succeeds(self, authenticated_user):
        """R4 AC2 — renaming to an unused name renames the folder, unchanged.
        A rename-only update carries no preset fields, so no preset is created."""
        folder = _create(authenticated_user, "Before")
        resp = authenticated_user.post(
            f"/api/v1/folders/{folder['id']}/update",
            json={"name": "After"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "After"

        refetch = authenticated_user.get(f"/api/v1/folders/{folder['id']}").json()
        assert refetch["name"] == "After"
        # A plain rename never implies or writes a preset.
        assert refetch["meta"] is None

    @pytest.mark.tier0
    def test_rename_missing_folder_404(self, authenticated_user):
        """R4 AC2 — renaming a nonexistent folder 404s, unchanged."""
        resp = authenticated_user.post(
            "/api/v1/folders/does-not-exist/update",
            json={"name": "Whatever"},
        )
        assert resp.status_code == 404

    # -- rename collision: the documented pair (T-003 fix) -----------------

    @pytest.mark.tier0
    def test_rename_to_different_taken_name_still_rejected(self, authenticated_user):
        """R4 AC2 — the REAL collision check is intact (part a of the documented
        pair): renaming a folder to a genuinely DIFFERENT name that is already
        taken by a sibling still 400s, exactly as before this feature."""
        _create(authenticated_user, "Occupied")
        mover = _create(authenticated_user, "Mover")

        resp = authenticated_user.post(
            f"/api/v1/folders/{mover['id']}/update",
            json={"name": "Occupied"},
        )
        assert resp.status_code == 400

        # The mover was not renamed by the rejected request.
        assert authenticated_user.get(f"/api/v1/folders/{mover['id']}").json()["name"] == "Mover"

    @pytest.mark.tier0
    def test_rename_to_own_current_name_no_ops(self, authenticated_user):
        """R4 AC2 — the documented, INTENTIONAL exception (part b of the pair,
        the T-003 fix): resubmitting a folder's OWN current name to ``/update``
        now succeeds and no-ops, where the original code incorrectly 400'd
        because its collision query compared the folder against itself. This is
        the fixed latent bug, NOT a regression to preserve — the test asserts
        the fixed behavior, not the old buggy one."""
        folder = _create(authenticated_user, "Same Name")

        resp = authenticated_user.post(
            f"/api/v1/folders/{folder['id']}/update",
            json={"name": "Same Name"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Same Name"

        refetch = authenticated_user.get(f"/api/v1/folders/{folder['id']}").json()
        assert refetch["name"] == "Same Name"
        # A same-name no-op writes nothing — no spurious preset gets created.
        assert refetch["meta"] is None

    # -- recursive subtree delete -----------------------------------------

    @pytest.mark.tier0
    def test_recursive_delete_removes_whole_subtree(self, authenticated_user):
        """R4 AC5 — deleting a folder recursively removes it and its entire
        descendant subtree, exactly as before this feature.

        Build parent → child → grandchild via create + reparent, then delete the
        parent and assert every node in the subtree is gone (404)."""
        parent = _create(authenticated_user, "Root")
        child = _create(authenticated_user, "Mid")
        grandchild = _create(authenticated_user, "Leaf")

        authenticated_user.post(
            f"/api/v1/folders/{child['id']}/update/parent",
            json={"parent_id": parent["id"]},
        )
        authenticated_user.post(
            f"/api/v1/folders/{grandchild['id']}/update/parent",
            json={"parent_id": child["id"]},
        )

        resp = authenticated_user.delete(f"/api/v1/folders/{parent['id']}")
        assert resp.status_code == 200

        # Every node in the subtree is gone.
        for node in (parent, child, grandchild):
            assert authenticated_user.get(f"/api/v1/folders/{node['id']}").status_code == 404

    @pytest.mark.tier0
    def test_recursive_delete_leaves_siblings_untouched(self, authenticated_user):
        """R4 AC5 — a recursive delete removes only the target subtree; unrelated
        sibling folders survive, unchanged."""
        parent = _create(authenticated_user, "DeleteMe")
        child = _create(authenticated_user, "DeleteMyChild")
        authenticated_user.post(
            f"/api/v1/folders/{child['id']}/update/parent",
            json={"parent_id": parent["id"]},
        )
        survivor = _create(authenticated_user, "KeepMe")

        assert authenticated_user.delete(f"/api/v1/folders/{parent['id']}").status_code == 200

        assert authenticated_user.get(f"/api/v1/folders/{parent['id']}").status_code == 404
        assert authenticated_user.get(f"/api/v1/folders/{child['id']}").status_code == 404
        # Untouched sibling still there.
        assert authenticated_user.get(f"/api/v1/folders/{survivor['id']}").status_code == 200

    @pytest.mark.tier0
    def test_delete_missing_folder_404(self, authenticated_user):
        """R4 AC5 — deleting a nonexistent folder 404s, unchanged."""
        assert authenticated_user.delete("/api/v1/folders/does-not-exist").status_code == 404


# ===========================================================================
# T-014 — pre-existing (pre-feature) presetless folders remain fully usable
# ===========================================================================


class TestLegacyPresetlessFolderRegression:
    """R4 AC6 — a folder created before this feature carries no ``meta``/preset.
    Reading it back returns no preset (not an error, not a spurious default), and
    it can still be updated/renamed/reparented/deleted normally.

    Each test seeds a legacy row directly (``meta = NULL``) to faithfully
    represent a folder that predates preset support, then drives it through the
    HTTP router owned by ``test_user`` (the user ``authenticated_user`` acts as).
    """

    @pytest.mark.tier0
    def test_legacy_folder_reads_back_with_no_preset(self, authenticated_user, test_user, db_session):
        """R4 AC6 — reading a pre-feature folder returns no preset: ``meta`` is
        null, never a synthesized empty preset and never an error."""
        fid = _seed_legacy_folder(db_session, test_user["id"], "Legacy Read")

        resp = authenticated_user.get(f"/api/v1/folders/{fid}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == fid
        assert body["name"] == "Legacy Read"
        # No preset implied — meta is null, not {"preset": ...}, not {}.
        assert body["meta"] is None

    @pytest.mark.tier0
    def test_legacy_folder_appears_in_list_with_no_preset(self, authenticated_user, test_user, db_session):
        """R4 AC6 — a pre-feature folder is listed normally, still carrying no
        preset."""
        fid = _seed_legacy_folder(db_session, test_user["id"], "Legacy Listed")

        listing = authenticated_user.get("/api/v1/folders/")
        assert listing.status_code == 200
        entry = next((f for f in listing.json() if f["id"] == fid), None)
        assert entry is not None
        assert entry["meta"] is None

    @pytest.mark.tier0
    def test_legacy_folder_can_be_renamed(self, authenticated_user, test_user, db_session):
        """R4 AC6 — a pre-feature folder renames normally, and the rename does
        not conjure a preset onto it."""
        fid = _seed_legacy_folder(db_session, test_user["id"], "Legacy Rename")

        resp = authenticated_user.post(
            f"/api/v1/folders/{fid}/update",
            json={"name": "Legacy Renamed"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Legacy Renamed"

        refetch = authenticated_user.get(f"/api/v1/folders/{fid}").json()
        assert refetch["name"] == "Legacy Renamed"
        assert refetch["meta"] is None  # still no preset implied

    @pytest.mark.tier0
    def test_legacy_folder_can_be_reparented(self, authenticated_user, test_user, db_session):
        """R4 AC6 — a pre-feature folder can be moved under another folder,
        unchanged."""
        fid = _seed_legacy_folder(db_session, test_user["id"], "Legacy Move")
        parent = _create(authenticated_user, "New Parent")

        resp = authenticated_user.post(
            f"/api/v1/folders/{fid}/update/parent",
            json={"parent_id": parent["id"]},
        )
        assert resp.status_code == 200
        assert resp.json()["parent_id"] == parent["id"]

    @pytest.mark.tier0
    def test_legacy_folder_can_be_expanded(self, authenticated_user, test_user, db_session):
        """R4 AC6 — a pre-feature folder can be expanded/collapsed, unchanged."""
        fid = _seed_legacy_folder(db_session, test_user["id"], "Legacy Expand")

        resp = authenticated_user.post(
            f"/api/v1/folders/{fid}/update/expanded",
            json={"is_expanded": True},
        )
        assert resp.status_code == 200
        assert resp.json()["is_expanded"] is True

    @pytest.mark.tier0
    def test_legacy_folder_can_be_deleted(self, authenticated_user, test_user, db_session):
        """R4 AC6 — a pre-feature folder deletes normally."""
        fid = _seed_legacy_folder(db_session, test_user["id"], "Legacy Delete")

        assert authenticated_user.delete(f"/api/v1/folders/{fid}").status_code == 200
        assert authenticated_user.get(f"/api/v1/folders/{fid}").status_code == 404

    @pytest.mark.tier0
    def test_legacy_folder_then_gets_a_valid_preset(self, authenticated_user, test_user, db_session):
        """R4 AC6 (usability) — a pre-feature presetless folder is not a
        second-class citizen: it can still receive a preset later through the
        same update path, proving the null-meta starting state is fully
        compatible with the new feature (merge onto null meta works)."""
        from selfai_ui.models.models import Model

        uid = test_user["id"]
        now = int(time.time())
        db_session.add(
            Model(
                id="legacy-upgrade-model",
                user_id=uid,
                base_model_id=None,
                name="legacy-upgrade-model",
                params={},
                meta={"description": "seed"},
                access_control=None,
                is_active=True,
                updated_at=now,
                created_at=now,
            )
        )
        db_session.commit()

        fid = _seed_legacy_folder(db_session, uid, "Legacy Upgrade")

        resp = authenticated_user.post(
            f"/api/v1/folders/{fid}/update",
            json={"name": "Legacy Upgrade", "default_model_id": "legacy-upgrade-model"},
        )
        assert resp.status_code == 200

        preset = authenticated_user.get(f"/api/v1/folders/{fid}").json()["meta"]["preset"]
        assert preset["default_model_id"] == "legacy-upgrade-model"
