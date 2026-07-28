"""End-user transcribe-picker empty/fallback state (cavekit-audio-transcribe-picker R5).

The end-user selectable list (T-031's ``GET /models/selectable``) returns only
the models an admin has enabled. On its own that is a truthful list, but it
cannot tell a picker *why* it is empty: an empty enabled-set could mean either

* **fallback** — models are downloaded and one is actively serving, but an admin
  has enabled none for explicit end-user selection. The picker must not present
  an empty, unselectable control; it falls back to the router's active model,
  which the Transcribe Router's active-model invariant
  (``cavekit-audio-transcribe-router.md`` R7) guarantees exists whenever any
  model is downloaded (R5 AC1); or
* **unavailable** — zero models are downloaded (the router's *permitted*
  "no active model" state, R7). There is nothing to serve and nothing to fall
  back to, so the picker shows a clear unavailable message rather than a broken
  or misleading control (R5 AC2).

When at least one model *is* enabled, the picker is in its normal **enabled**
mode and offers exactly those models (R5 AC3 — enabling a model transitions the
picker out of the fallback/unavailable states into normal selection).

This module is the *pure* resolver of those three states. It composes T-031's
:func:`selfai_ui.transcribe.listing.filter_enabled_models` (the enabled-only
list) with T-025's :func:`selfai_ui.transcribe.invariant.inspect_active_model`
(the active-model resolver) over a single T-020 listing, so the empty-state
policy lives in one testable place and the router route stays a thin adapter.
It reads config for neither and mutates nothing.
"""

from typing import Any, Optional

from selfai_ui.transcribe.invariant import inspect_active_model
from selfai_ui.transcribe.listing import filter_enabled_models

# The three end-user picker states, as stable JSON literals a client can branch
# on. Kept as plain strings (not an Enum) to match the listing kit's convention.
SELECTABLE_MODE_ENABLED = "enabled"
SELECTABLE_MODE_FALLBACK = "fallback"
SELECTABLE_MODE_UNAVAILABLE = "unavailable"

# Message shown when zero models are downloaded (R5 AC2). A clear unavailable
# statement, not a control — the picker renders this instead of an empty select.
UNAVAILABLE_MESSAGE = (
    "No transcription models are available yet. An administrator must download "
    "and enable a model before one can be selected."
)


def _entry_for(listing: dict, model_id: Optional[str]) -> Optional[dict]:
    """Return a copy of the listing entry whose id is ``model_id``, or None."""
    if not model_id:
        return None
    for entry in listing.get("data", []):
        if isinstance(entry, dict) and entry.get("id") == model_id:
            return dict(entry)
    return None


def resolve_selectable_state(listing: dict, enabled_map: Any) -> dict:
    """Resolve the end-user picker's selectable state from a listing (R5).

    Composes the enabled-only list and the active-model resolver over a single
    T-020 ``listing`` and returns a self-describing envelope:

    ``{"data": [...], "mode": <str>, "active_model": <id|None>,
       "fallback_model": <entry|None>, "message": <str|None>}``

    * ``data`` — the enabled-only selectable list (T-031 contract, unchanged):
      the models an end user may explicitly select. Empty in the fallback and
      unavailable modes.
    * ``mode`` — one of :data:`SELECTABLE_MODE_ENABLED` /
      :data:`SELECTABLE_MODE_FALLBACK` / :data:`SELECTABLE_MODE_UNAVAILABLE`.
    * ``active_model`` — the router's active model id, when one is resolvable
      (present in both the enabled and fallback modes when a model is serving).
    * ``fallback_model`` — in fallback mode, the full listing entry of the active
      model the picker falls back to, so the picker can name it without an
      admin-only call; None otherwise.
    * ``message`` — a human-readable unavailable statement in the unavailable
      mode; None otherwise.

    State selection (R5):

    * At least one enabled model -> **enabled** (normal selection, AC3).
    * None enabled but a model is downloaded and actively serving -> **fallback**
      to that active model (AC1); the R7 invariant guarantees the active model
      exists whenever any model is downloaded.
    * Otherwise (zero downloaded, the router's permitted no-active state) ->
      **unavailable** with a clear message (AC2).

    Defensive note: if the backend anomalously reports models downloaded but none
    active (an R7 violation :func:`inspect_active_model` flags with ``ok=False``),
    there is no active model to fall back to, so this resolver reports
    **unavailable** rather than inventing a fallback target — it never presents a
    control pointing at a model that is not actually serving.
    """
    selectable = filter_enabled_models(listing, enabled_map).get("data", [])
    state = inspect_active_model(listing)
    active_id = state.active_id

    if selectable:
        return {
            "data": selectable,
            "mode": SELECTABLE_MODE_ENABLED,
            "active_model": active_id,
            "fallback_model": None,
            "message": None,
        }

    # No model is admin-enabled. Fall back to the router's active model if one is
    # actually serving (R7 guarantees this whenever any model is downloaded).
    if active_id is not None:
        return {
            "data": [],
            "mode": SELECTABLE_MODE_FALLBACK,
            "active_model": active_id,
            "fallback_model": _entry_for(listing, active_id),
            "message": None,
        }

    # Zero downloaded (or an anomalous no-active state): nothing to serve and
    # nothing to fall back to -> a clear unavailable message, not a control.
    return {
        "data": [],
        "mode": SELECTABLE_MODE_UNAVAILABLE,
        "active_model": None,
        "fallback_model": None,
        "message": UNAVAILABLE_MESSAGE,
    }
