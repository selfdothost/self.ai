"""Transcribe-router model listing (cavekit-audio-transcribe-router R1).

Pure, backend-agnostic merge logic that combines two views reported by the
self-hosted STT backend's control port into a single listing where every entry
is unambiguously either *downloaded-and-ready* or *merely pullable*:

* **downloaded** — the model is present on the backend and ready to serve a
  transcription request (it may still need loading, but no download is
  required).
* **pullable** — a known catalog entry the backend advertises as available to
  download, but which is not currently present.

The two inputs mirror self.llamolotl's control-port mechanics:

* ``downloaded_raw`` — the backend's list of models it actually holds
  (``{control}/api/models`` — the STT analog of llamolotl's ``list_models``).
* ``catalog_raw`` — the backend's advertised catalog of known models
  (``{control}/api/models/available`` — the STT analog of llamolotl's
  ``/api/models/available``).

Matching between the two views is by model id (see ``_entry_id``). A catalog
entry whose id also appears in the downloaded view is reported as
``downloaded`` (catalog metadata such as a description is merged in); a catalog
entry with no downloaded counterpart is reported as ``pullable``. A downloaded
model that is not a known catalog entry is still reported as ``downloaded``.
"""

from typing import Any, Optional

from pydantic import BaseModel

# Availability discriminator values. Kept as plain strings (not an Enum) so the
# JSON contract is a stable, self-describing literal for every consumer.
AVAILABILITY_DOWNLOADED = "downloaded"
AVAILABILITY_PULLABLE = "pullable"


class TranscribeModelEntry(BaseModel):
    """One entry in the transcribe router's model listing.

    ``downloaded`` (bool) and ``availability`` (str) each independently make a
    downloaded-and-ready model distinguishable from a merely-pullable one — the
    two are redundant on purpose so a consumer can branch on whichever it finds
    more natural without ambiguity.
    """

    id: str
    name: str
    # True iff the model is present on the backend and ready to serve.
    downloaded: bool
    # AVAILABILITY_DOWNLOADED or AVAILABILITY_PULLABLE — the discriminator.
    availability: str
    # Backend-reported serving status for a downloaded model (e.g. "loaded",
    # "unloaded"), when the backend supplies one. None for pullable entries.
    status: Optional[str] = None
    # Human-readable catalog description, when the catalog supplies one.
    description: Optional[str] = None
    # Admin-curation flag (cavekit-audio-transcribe-picker R1): True iff an admin
    # has enabled this model for end-user selection. Independent of ``downloaded``
    # — a model may be enabled while merely pullable. Defaults False (an admin
    # curates models in). Optional so the base listing contract (T-020) is
    # unchanged for any consumer that does not read curation state.
    enabled: bool = False
    # Active-model indicator (cavekit-audio-transcribe-picker R3): True iff this is
    # the single model the router currently has resident/serving. Strictly narrower
    # than ``downloaded`` — only a downloaded-and-ready model can be active — so the
    # active marker is distinct from the downloaded indicator (R3 AC2). The base
    # listing cannot know the active model on its own, so this defaults False and is
    # set by ``invariant.mark_active_model`` (which resolves the active model from
    # each entry's backend-reported ``status``); the default keeps the T-020
    # contract unchanged for any consumer that does not read active state.
    active: bool = False


def _as_list(raw: Any) -> list[dict]:
    """Coerce a backend response into a list of dict rows.

    Accepts either a bare list, or a dict wrapping the rows under ``data`` or
    ``models`` (both shapes appear across the platform's backends). Anything
    else yields an empty list rather than raising — a malformed or missing view
    must not break the listing.
    """
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict):
        inner = raw.get("data")
        if inner is None:
            inner = raw.get("models")
        rows = inner if isinstance(inner, list) else []
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


def _entry_id(row: dict) -> Optional[str]:
    """Extract the model identifier used to match across the two views."""
    for key in ("id", "name", "model"):
        val = row.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def _status_of(row: dict) -> Optional[str]:
    """Extract a serving status string from a downloaded-model row, if any.

    Tolerates both a flat ``status: "loaded"`` and llamolotl's nested
    ``status: {"value": "loaded"}`` shape.
    """
    status = row.get("status")
    if isinstance(status, dict):
        val = status.get("value")
        return val if isinstance(val, str) else None
    if isinstance(status, str):
        return status
    return None


def _display_name(row: dict, model_id: str) -> str:
    name = row.get("name")
    if isinstance(name, str) and name:
        return name
    return model_id


def _description_of(row: dict) -> Optional[str]:
    desc = row.get("description")
    return desc if isinstance(desc, str) and desc else None


def build_model_listing(downloaded_raw: Any, catalog_raw: Any) -> dict:
    """Merge the downloaded and catalog views into a single listing.

    Returns ``{"data": [entry, ...]}`` where each entry is a
    :class:`TranscribeModelEntry` serialized to a dict. Downloaded models are
    listed first (in the backend's order), then pullable-only catalog entries.
    """
    downloaded_rows = _as_list(downloaded_raw)
    catalog_rows = _as_list(catalog_raw)

    # Index catalog rows by id so downloaded entries can inherit catalog
    # metadata (e.g. a description) and so we can tell which catalog entries are
    # NOT downloaded.
    catalog_by_id: dict[str, dict] = {}
    for row in catalog_rows:
        cid = _entry_id(row)
        if cid and cid not in catalog_by_id:
            catalog_by_id[cid] = row

    entries: list[TranscribeModelEntry] = []
    seen: set[str] = set()

    # 1) Everything the backend actually holds -> downloaded-and-ready.
    for row in downloaded_rows:
        model_id = _entry_id(row)
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        catalog_row = catalog_by_id.get(model_id, {})
        entries.append(
            TranscribeModelEntry(
                id=model_id,
                name=_display_name(row, model_id),
                downloaded=True,
                availability=AVAILABILITY_DOWNLOADED,
                status=_status_of(row),
                description=_description_of(row) or _description_of(catalog_row),
            )
        )

    # 2) Known catalog entries with no downloaded counterpart -> pullable.
    for model_id, row in catalog_by_id.items():
        if model_id in seen:
            continue
        seen.add(model_id)
        entries.append(
            TranscribeModelEntry(
                id=model_id,
                name=_display_name(row, model_id),
                downloaded=False,
                availability=AVAILABILITY_PULLABLE,
                status=None,
                description=_description_of(row),
            )
        )

    return {"data": [entry.model_dump() for entry in entries]}


def _matches_search(entry: dict, needle: str) -> bool:
    """True iff the search needle appears in the entry's id, name or description.

    Case-insensitive substring match, consistent with the established
    curation-list search pattern (a plain text filter over the visible fields).
    """
    for key in ("id", "name", "description"):
        val = entry.get(key)
        if isinstance(val, str) and needle in val.lower():
            return True
    return False


def curate_listing(
    listing: dict,
    enabled_map: Any,
    search: Optional[str] = None,
) -> dict:
    """Overlay admin curation state onto a base listing and apply search.

    Adds the ``enabled`` flag to every entry from ``enabled_map`` (a
    model-id -> bool map; an id absent from the map is treated as not enabled),
    then, if ``search`` is a non-empty string, filters the entries to those whose
    id, name or description contain it (case-insensitive). Pure: it neither reads
    config nor mutates its inputs, so it is directly unit-testable.

    Both downloaded and pullable entries are carried through and independently
    curatable — enabling a merely-pullable model is permitted.
    """
    enabled = enabled_map if isinstance(enabled_map, dict) else {}
    needle = search.strip().lower() if isinstance(search, str) else ""

    curated: list[dict] = []
    for entry in listing.get("data", []):
        entry = dict(entry)
        entry["enabled"] = bool(enabled.get(entry.get("id"), False))
        if needle and not _matches_search(entry, needle):
            continue
        curated.append(entry)

    return {"data": curated}


def filter_enabled_models(listing: dict, enabled_map: Any) -> dict:
    """Return only the entries an admin has enabled for end-user selection.

    The end-user counterpart to :func:`curate_listing`
    (cavekit-audio-transcribe-picker R4): where the admin curation view lists
    *every* model with its ``enabled`` flag, this returns *only* the models the
    admin has explicitly enabled — precisely those whose id maps to ``True`` in
    ``enabled_map``. An id that is absent, ``False``, or any non-``True`` value
    is excluded, so a model an admin has disabled is never selectable (R4 AC3).

    Pure: it neither reads config nor mutates its inputs, mirroring
    :func:`curate_listing` so the same listing/enabled-map machinery T-028 built
    is reused, only filtered down. Both downloaded and (admin-enabled) pullable
    entries pass through; each returned entry carries ``enabled=True``.
    """
    enabled = enabled_map if isinstance(enabled_map, dict) else {}

    selectable: list[dict] = []
    for entry in listing.get("data", []):
        if enabled.get(entry.get("id")) is True:
            entry = dict(entry)
            entry["enabled"] = True
            selectable.append(entry)

    return {"data": selectable}
