"""Reference-validation helpers for chat-folder presets (cavekit R3).

A folder preset (``FolderPresetModel``) carries three kinds of references — a
default model id, a list of tool ids, and a list of knowledge ids. Before a
preset is written (wired in a later task, T-007), every reference it carries
must resolve to a *real record that is accessible to the writing user*.

The single security-critical property here (cavekit R3): a reference to a
record that **exists but is outside the writer's access scope** must resolve
**identically** to a reference that does not exist at all. Both are simply
"does not resolve" — the caller cannot tell existence from inaccessibility.
This is achieved by fetching each record with the unscoped ``get_*_by_id``
accessor and then applying the *same* access gate the routers use
(``routers/models.py``, ``routers/tools.py``, ``routers/knowledge.py``):

    admin  OR  record.user_id == user.id  OR  has_access(user.id, permission, record.access_control)

Both the "record is None" case and the "gate returns False" case collapse to
the same boolean, so no existence signal leaks through the return shape.

This module is resolution-only. Wiring it into the folder-update path and
raising an atomic rejection before commit is a separate task (T-007); this
module never touches the database write path and never raises on an
unresolved reference — it reports.
"""

from typing import Any

from selfai_ui.models.folders import FolderPresetModel
from selfai_ui.models.knowledge import Knowledges
from selfai_ui.models.models import Models
from selfai_ui.models.tools import Tools
from selfai_ui.utils.access_control import has_access

# Reference kinds, used as the first element of each unresolved-reference tuple
# so the caller can report *which* field failed without leaking existence.
REF_MODEL = "model"
REF_TOOL = "tool"
REF_KNOWLEDGE = "knowledge"


def _record_accessible(record: Any, user: Any, permission: str) -> bool:
    """Apply the router access gate to an already-fetched record.

    A ``None`` record (nonexistent) and a record the user cannot reach
    (inaccessible) both return ``False`` — indistinguishable by design.
    """
    if record is None:
        return False
    if getattr(user, "role", None) == "admin":
        return True
    if record.user_id == user.id:
        return True
    return has_access(user.id, permission, record.access_control)


def resolve_model_ref(model_id: str, user: Any, permission: str = "read") -> bool:
    """Return ``True`` iff ``model_id`` resolves to a model accessible to ``user``."""
    return _record_accessible(Models.get_model_by_id(model_id), user, permission)


def resolve_tool_ref(tool_id: str, user: Any, permission: str = "read") -> bool:
    """Return ``True`` iff ``tool_id`` resolves to a tool accessible to ``user``."""
    return _record_accessible(Tools.get_tool_by_id(tool_id), user, permission)


def resolve_knowledge_ref(knowledge_id: str, user: Any, permission: str = "read") -> bool:
    """Return ``True`` iff ``knowledge_id`` resolves to a knowledge record accessible to ``user``."""
    return _record_accessible(Knowledges.get_knowledge_by_id(knowledge_id), user, permission)


def unresolved_preset_references(
    preset: FolderPresetModel,
    user: Any,
    permission: str = "read",
) -> list[tuple[str, str]]:
    """Return every preset reference that does NOT resolve to an accessible record.

    The result is a list of ``(kind, id)`` tuples, where ``kind`` is one of
    ``REF_MODEL`` / ``REF_TOOL`` / ``REF_KNOWLEDGE``. An empty list means every
    reference the preset carries resolved to a real, accessible record (and a
    preset carrying no references trivially yields an empty list).

    Nonexistent and inaccessible references produce the *same* tuple shape, so
    a caller (T-007) rejecting the write on a non-empty result never
    distinguishes "does not exist" from "not allowed" — satisfying the R3
    no-existence-leak guarantee.
    """
    unresolved: list[tuple[str, str]] = []

    if preset.default_model_id is not None:
        if not resolve_model_ref(preset.default_model_id, user, permission):
            unresolved.append((REF_MODEL, preset.default_model_id))

    for tool_id in preset.tool_ids or []:
        if not resolve_tool_ref(tool_id, user, permission):
            unresolved.append((REF_TOOL, tool_id))

    for knowledge_id in preset.knowledge_ids or []:
        if not resolve_knowledge_ref(knowledge_id, user, permission):
            unresolved.append((REF_KNOWLEDGE, knowledge_id))

    return unresolved


def preset_references_resolve(
    preset: FolderPresetModel,
    user: Any,
    permission: str = "read",
) -> bool:
    """Convenience: ``True`` iff every reference in ``preset`` is accessible to ``user``."""
    return not unresolved_preset_references(preset, user, permission)
