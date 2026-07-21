"""Unit tests for the folder-preset reference-validation helper (cavekit R3).

These tests patch the record accessors (``Models.get_model_by_id`` etc.) and
``has_access`` at the helper's own module namespace, so no database, FastAPI
app, or real access-control graph is required — the resolution logic and, most
importantly, the *no-existence-leak* property are exercised in isolation.

Run from the backend container:
    pytest tests/utils/test_folder_presets.py -v
"""

from types import SimpleNamespace
from unittest.mock import patch

from selfai_ui.models.folders import FolderPresetModel
from selfai_ui.utils import folder_presets as fp

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user(user_id="writer", role="user"):
    """A stand-in for the verified UserModel (only .id and .role are read)."""
    return SimpleNamespace(id=user_id, role=role)


def _record(user_id="owner", access_control=None):
    """A stand-in for a Model/Tool/Knowledge record (only .user_id and
    .access_control are read by the access gate)."""
    return SimpleNamespace(user_id=user_id, access_control=access_control)


# ---------------------------------------------------------------------------
# Per-kind resolution: accessible cases
# ---------------------------------------------------------------------------


def test_model_ref_owned_by_user_resolves():
    with patch.object(fp.Models, "get_model_by_id", return_value=_record(user_id="writer")):
        assert fp.resolve_model_ref("m1", _user("writer")) is True


def test_tool_ref_owned_by_user_resolves():
    with patch.object(fp.Tools, "get_tool_by_id", return_value=_record(user_id="writer")):
        assert fp.resolve_tool_ref("t1", _user("writer")) is True


def test_knowledge_ref_owned_by_user_resolves():
    with patch.object(fp.Knowledges, "get_knowledge_by_id", return_value=_record(user_id="writer")):
        assert fp.resolve_knowledge_ref("k1", _user("writer")) is True


def test_ref_accessible_via_has_access_resolves():
    # Not owned by the user, but has_access grants it.
    rec = _record(user_id="someone_else", access_control={"write": {"user_ids": ["writer"]}})
    with patch.object(fp.Models, "get_model_by_id", return_value=rec), patch.object(
        fp, "has_access", return_value=True
    ) as mock_access:
        assert fp.resolve_model_ref("m1", _user("writer")) is True
        mock_access.assert_called_once()


def test_admin_resolves_any_existing_record():
    # Admin bypasses ownership/has_access, mirroring the router gate.
    rec = _record(user_id="someone_else", access_control=None)
    with patch.object(fp.Tools, "get_tool_by_id", return_value=rec), patch.object(
        fp, "has_access", return_value=False
    ):
        assert fp.resolve_tool_ref("t1", _user("admin_user", role="admin")) is True


# ---------------------------------------------------------------------------
# The R3 no-existence-leak property
# ---------------------------------------------------------------------------


def test_nonexistent_ref_does_not_resolve():
    with patch.object(fp.Models, "get_model_by_id", return_value=None):
        assert fp.resolve_model_ref("ghost", _user("writer")) is False


def test_inaccessible_ref_does_not_resolve():
    # Record exists, owned by another user, and has_access denies it.
    rec = _record(user_id="someone_else", access_control={"write": {"user_ids": ["other"]}})
    with patch.object(fp.Models, "get_model_by_id", return_value=rec), patch.object(
        fp, "has_access", return_value=False
    ):
        assert fp.resolve_model_ref("locked", _user("writer")) is False


def test_inaccessible_resolves_identically_to_nonexistent():
    """The core R3 guarantee: existing-but-inaccessible is indistinguishable
    from nonexistent — same boolean from the resolver, and byte-identical
    unresolved-reference shape from the aggregate helper."""
    user = _user("writer")
    preset = FolderPresetModel(default_model_id="x")

    # Case A: reference does not exist at all.
    with patch.object(fp.Models, "get_model_by_id", return_value=None):
        resolve_nonexistent = fp.resolve_model_ref("x", user)
        unresolved_nonexistent = fp.unresolved_preset_references(preset, user)

    # Case B: reference exists but is outside the writer's access scope.
    rec = _record(user_id="someone_else", access_control={"write": {"user_ids": ["other"]}})
    with patch.object(fp.Models, "get_model_by_id", return_value=rec), patch.object(
        fp, "has_access", return_value=False
    ):
        resolve_inaccessible = fp.resolve_model_ref("x", user)
        unresolved_inaccessible = fp.unresolved_preset_references(preset, user)

    # Identical resolver verdict.
    assert resolve_nonexistent == resolve_inaccessible == False  # noqa: E712
    # Identical aggregate shape — no leak of existence vs. inaccessibility.
    assert unresolved_nonexistent == unresolved_inaccessible == [(fp.REF_MODEL, "x")]


# ---------------------------------------------------------------------------
# Aggregate: unresolved_preset_references / preset_references_resolve
# ---------------------------------------------------------------------------


def test_empty_preset_has_no_unresolved_references():
    preset = FolderPresetModel()
    assert fp.unresolved_preset_references(preset, _user()) == []
    assert fp.preset_references_resolve(preset, _user()) is True


def test_fully_accessible_preset_resolves():
    preset = FolderPresetModel(
        default_model_id="m1",
        tool_ids=["t1", "t2"],
        knowledge_ids=["k1"],
    )
    with patch.object(fp.Models, "get_model_by_id", return_value=_record(user_id="writer")), patch.object(
        fp.Tools, "get_tool_by_id", return_value=_record(user_id="writer")
    ), patch.object(fp.Knowledges, "get_knowledge_by_id", return_value=_record(user_id="writer")):
        assert fp.unresolved_preset_references(preset, _user("writer")) == []
        assert fp.preset_references_resolve(preset, _user("writer")) is True


def test_each_kind_reported_when_unresolved():
    preset = FolderPresetModel(
        default_model_id="m_bad",
        tool_ids=["t_ok", "t_bad"],
        knowledge_ids=["k_bad"],
    )

    def model_lookup(_id):
        return None  # model unresolved

    def tool_lookup(_id):
        return _record(user_id="writer") if _id == "t_ok" else None

    def knowledge_lookup(_id):
        return None  # knowledge unresolved

    with patch.object(fp.Models, "get_model_by_id", side_effect=model_lookup), patch.object(
        fp.Tools, "get_tool_by_id", side_effect=tool_lookup
    ), patch.object(fp.Knowledges, "get_knowledge_by_id", side_effect=knowledge_lookup):
        unresolved = fp.unresolved_preset_references(preset, _user("writer"))

    assert unresolved == [
        (fp.REF_MODEL, "m_bad"),
        (fp.REF_TOOL, "t_bad"),
        (fp.REF_KNOWLEDGE, "k_bad"),
    ]
    assert fp.preset_references_resolve(preset, _user("writer")) is False


def test_permission_argument_is_forwarded_to_has_access():
    rec = _record(user_id="someone_else", access_control={"read": {"user_ids": ["writer"]}})
    with patch.object(fp.Knowledges, "get_knowledge_by_id", return_value=rec), patch.object(
        fp, "has_access", return_value=True
    ) as mock_access:
        assert fp.resolve_knowledge_ref("k1", _user("writer"), permission="read") is True
        # user_id, permission, access_control forwarded through to the real gate.
        args, _ = mock_access.call_args
        assert args[0] == "writer"
        assert args[1] == "read"
        assert args[2] == rec.access_control
