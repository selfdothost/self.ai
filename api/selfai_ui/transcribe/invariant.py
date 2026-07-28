"""Active-model invariant over the transcribe router (cavekit-audio-transcribe-router R7).

R7 is a *cross-cutting invariant*, not a single operation: whenever at least one
model is downloaded, exactly one downloaded model must be the active one that
serves transcription. A "no active model" state is legitimate **only** when zero
models are downloaded. No listing, pull (T-021), swap (T-024) or deletion (T-026)
may leave the router with downloaded models but none active.

This module is the *pure* home of that invariant. It reads a T-020 listing
(:func:`selfai_ui.transcribe.listing.build_model_listing`) and, from each
downloaded entry's backend-reported ``status`` field, determines which model —
if any — is active. It offers three things the rest of the domain composes with:

* **Reporting** (AC1/AC3) — :func:`inspect_active_model` / :func:`active_model_id`
  resolve the single active model, or an explicit "no active model" state.
* **A deletion gate** (AC2) — :func:`assert_deletable` raises if the target is the
  currently-active model. T-026 (deletion, a later tier that depends on THIS task)
  MUST call this before removing a model.
* **A verification layer** (AC4) — :func:`check_active_model_invariant` /
  :func:`assert_operation_preserves_invariant` prove that a resulting listing (or
  a before/after pair) upholds the invariant, without modifying the already-merged
  pull/swap operations.

The active-status signal
------------------------
The backend's control port reports a serving ``status`` per downloaded model
(the STT analog of self.llamolotl's loaded/unloaded state). ``build_model_listing``
already surfaces it verbatim on each entry as ``status`` (e.g. ``"loaded"`` /
``"unloaded"``), and T-024's activation returns ``status: "active"`` for the model
it just loaded. We therefore treat both ``"loaded"`` and ``"active"`` (case- and
whitespace-insensitive) as "this model is the one serving" — see
:data:`ACTIVE_STATUSES`.

Anomaly policy (backend bug / race)
-----------------------------------
The single-active-model backend *should* report exactly one loaded model whenever
any are downloaded. It might not — a bug or race could report **zero** loaded (with
models present) or **multiple** loaded at once. This module surfaces either as a
first-class data-integrity anomaly rather than hiding it:

* **Zero active, models present** → invariant *violated*. :func:`inspect_active_model`
  returns ``active_id=None`` with ``ok=False`` and a human-readable ``anomaly``.
  :func:`establish_target` names the model a caller should activate to repair it.
* **Multiple active** → invariant *violated*, but a listing consumer still needs a
  single answer, so we deterministically pick the **first** active model in listing
  order for ``active_id`` while flagging ``ok=False`` with an ``anomaly``. Picking
  (rather than raising) keeps read paths — the picker's active indicator, T-030 —
  functioning under a transient backend hiccup instead of hard-failing them, while
  :func:`check_active_model_invariant` still lets a caller that wants strictness
  raise.
"""

from dataclasses import dataclass
from typing import Optional

from selfai_ui.transcribe.listing import AVAILABILITY_DOWNLOADED

# Backend-reported serving statuses that mean "this downloaded model is the one
# currently serving transcription". Compared case- and whitespace-insensitively.
# "loaded" is llamolotl's resident-model status surfaced by build_model_listing;
# "active" is what T-024's activate endpoint returns for the model it just loaded.
ACTIVE_STATUSES = frozenset({"loaded", "active"})


class ActiveModelInvariantError(Exception):
    """The active-model invariant (R7) is violated by a listing.

    Raised by :func:`check_active_model_invariant` /
    :func:`assert_operation_preserves_invariant` when one or more models are
    downloaded but the backend does not report exactly one of them active.
    """

    def __init__(self, message: str):
        super().__init__(message)


class ModelIsActiveError(Exception):
    """A deletion targeted the currently-active model (R7 AC2 / R5).

    Deletion of the active model is refused while it remains active — it must
    first be swapped (R4/T-024) to another downloaded model. T-026's delete path
    surfaces this as a client-facing rejection. Raised by :func:`assert_deletable`.
    """

    def __init__(self, model_id: str):
        self.model_id = model_id
        super().__init__(
            f"model {model_id!r} is the active transcription model and cannot be "
            "deleted while active; swap the active model to another downloaded "
            "model first"
        )


def _norm_status(status: Optional[str]) -> str:
    """Normalize a status string for comparison (lowercased, trimmed)."""
    return status.strip().lower() if isinstance(status, str) else ""


def downloaded_ids_in_order(listing: dict) -> list[str]:
    """Return the downloaded-and-ready model ids, in listing order.

    Uses the same both-flags guard as :func:`selfai_ui.transcribe.swap.downloaded_ready_ids`
    (an entry must be *both* ``downloaded is True`` and carry the ``downloaded``
    availability discriminator) so a malformed entry can never be counted, but
    preserves order — the invariant needs a deterministic "first" model.
    """
    ids: list[str] = []
    for entry in listing.get("data", []):
        if not isinstance(entry, dict):
            continue
        if entry.get("downloaded") is True and entry.get("availability") == AVAILABILITY_DOWNLOADED:
            model_id = entry.get("id")
            if isinstance(model_id, str) and model_id:
                ids.append(model_id)
    return ids


def reported_active_ids(listing: dict) -> list[str]:
    """Return downloaded ids the backend reports as active, in listing order.

    A model qualifies only if it is downloaded-and-ready *and* its ``status`` is
    one of :data:`ACTIVE_STATUSES`. May be empty, one, or (anomalously) many.
    """
    active: list[str] = []
    for entry in listing.get("data", []):
        if not isinstance(entry, dict):
            continue
        if entry.get("downloaded") is not True or entry.get("availability") != AVAILABILITY_DOWNLOADED:
            continue
        model_id = entry.get("id")
        if not (isinstance(model_id, str) and model_id):
            continue
        if _norm_status(entry.get("status")) in ACTIVE_STATUSES:
            active.append(model_id)
    return active


@dataclass(frozen=True)
class ActiveModelState:
    """The resolved active-model state of a listing (R7).

    ``active_id`` is the single model serving transcription, or ``None``. It is
    ``None`` legitimately only when no models are downloaded (``ok`` True); a
    ``None`` with ``ok`` False means the invariant is violated (models present,
    none active). When multiple are anomalously reported active, ``active_id`` is
    the first in listing order and ``ok`` is False.
    """

    active_id: Optional[str]
    downloaded_ids: tuple[str, ...]
    reported_active_ids: tuple[str, ...]
    ok: bool
    anomaly: Optional[str]

    @property
    def has_active(self) -> bool:
        return self.active_id is not None

    @property
    def has_downloaded(self) -> bool:
        return len(self.downloaded_ids) > 0


def inspect_active_model(listing: dict) -> ActiveModelState:
    """Resolve the active-model state of a listing (R7 AC1/AC3, anomaly-aware).

    * No models downloaded -> ``active_id=None``, ``ok=True`` (the legitimate
      "no active model" state, AC3).
    * Exactly one active -> ``active_id`` set, ``ok=True`` (the healthy AC1 state).
    * Models downloaded but none active -> ``active_id=None``, ``ok=False``
      (invariant violated; :func:`establish_target` names the repair, AC4).
    * Multiple active -> first in order chosen for ``active_id``, ``ok=False``
      (anomaly surfaced; read paths keep working — see module docstring).
    """
    downloaded = tuple(downloaded_ids_in_order(listing))
    active = tuple(reported_active_ids(listing))

    if not downloaded:
        return ActiveModelState(
            active_id=None,
            downloaded_ids=downloaded,
            reported_active_ids=active,
            ok=True,
            anomaly=None,
        )

    if len(active) == 1:
        return ActiveModelState(
            active_id=active[0],
            downloaded_ids=downloaded,
            reported_active_ids=active,
            ok=True,
            anomaly=None,
        )

    if len(active) == 0:
        return ActiveModelState(
            active_id=None,
            downloaded_ids=downloaded,
            reported_active_ids=active,
            ok=False,
            anomaly=(
                f"{len(downloaded)} model(s) downloaded but the backend reports "
                "none active; the invariant requires exactly one active model "
                "whenever any are downloaded"
            ),
        )

    # len(active) > 1: pick the first deterministically, flag the anomaly.
    return ActiveModelState(
        active_id=active[0],
        downloaded_ids=downloaded,
        reported_active_ids=active,
        ok=False,
        anomaly=(
            "backend reports multiple active models "
            f"({', '.join(active)}); expected exactly one — using the first"
        ),
    )


def active_model_id(listing: dict) -> Optional[str]:
    """Return the single active model id, or ``None`` (R7 AC1/AC3).

    Convenience wrapper over :func:`inspect_active_model`. ``None`` means either
    no models are downloaded (legitimate) or the invariant is violated; use
    :func:`inspect_active_model` when the caller must tell those apart.
    """
    return inspect_active_model(listing).active_id


def has_active_model(listing: dict) -> bool:
    """True iff some downloaded model is reported active."""
    return inspect_active_model(listing).has_active


def mark_active_model(listing: dict) -> dict:
    """Overlay an ``active`` flag on each listing entry (transcribe-picker R3).

    Marks the single resident/serving model — resolved by :func:`active_model_id`
    from each downloaded entry's backend-reported serving ``status`` — with
    ``active=True`` and every other entry with ``active=False``. This is the
    picker's active-model indicator (R3 AC1): a curation-list consumer can render a
    distinct "active" marker straight off the listing response, with no second
    call.

    ``active`` is strictly narrower than ``downloaded``: only a downloaded-and-ready
    model can be reported active (a merely-pullable entry never is, since
    :func:`reported_active_ids` considers only downloaded entries), so the flag
    marks a model as *more than just downloaded* — distinct from the
    ``downloaded`` / ``availability`` indicator (R3 AC2).

    The function holds no state of its own: it reads the active model out of the
    listing each call. So when the backend's active model changes, a freshly built
    listing resolves a different ``active_id`` and the flag moves to the new model
    on reload (R3 AC3). When no model is active — the legitimate zero-downloaded
    state, or an anomalous downloaded-but-none-active one — every entry is marked
    ``active=False``.

    Pure: copies each entry, mutates nothing in place. A non-dict entry is passed
    through untouched. Composes freely with the T-020 base listing and the T-028
    :func:`selfai_ui.transcribe.listing.curate_listing` overlay — each writes a
    different key, so order does not matter.
    """
    active_id = active_model_id(listing)
    marked: list = []
    for entry in listing.get("data", []):
        if isinstance(entry, dict):
            entry = dict(entry)
            entry["active"] = active_id is not None and entry.get("id") == active_id
        marked.append(entry)
    return {"data": marked}


def establish_target(listing: dict) -> Optional[str]:
    """Name the model to activate to *repair* a violated invariant, if any.

    When the listing has downloaded models but none active (the AC4 violation a
    first pull can produce, since a pull does not itself activate anything), this
    returns the first downloaded id — a caller can activate it (via T-024's swap)
    to re-establish the invariant. Returns ``None`` when nothing needs
    establishing (no models downloaded, or one is already active).
    """
    state = inspect_active_model(listing)
    if not state.has_downloaded:
        return None
    if state.reported_active_ids:  # already one-or-more active (healthy or multi-anomaly)
        return None
    return state.downloaded_ids[0]


def check_active_model_invariant(listing: dict) -> None:
    """Raise if a listing violates the active-model invariant (R7 AC4).

    A strict gate for callers that want the invariant enforced: raises
    :class:`ActiveModelInvariantError` when models are downloaded but the backend
    does not report exactly one active. A no-models listing passes (AC3).
    """
    state = inspect_active_model(listing)
    if not state.ok:
        raise ActiveModelInvariantError(state.anomaly or "active-model invariant violated")


def assert_operation_preserves_invariant(before: dict, after: dict) -> None:
    """Prove a pull/swap/delete did not violate the invariant (R7 AC4).

    The invariant constrains the *resulting* state, so this asserts ``after``
    upholds it via :func:`check_active_model_invariant`. ``before`` is accepted so
    call sites read as "operation(before) -> after" and so future checks (e.g. a
    swap actually moved the active model) have it available; it is not required to
    itself be valid — an operation may *repair* a previously-violated state.

    :raises ActiveModelInvariantError: if ``after`` leaves models downloaded but
        not exactly one active.
    """
    check_active_model_invariant(after)


def assert_deletable(listing: dict, model_id: str) -> None:
    """Gate a deletion on the active-model invariant (R7 AC2 / R5).

    T-026's delete path MUST call this before removing ``model_id``. Deleting the
    currently-active model is refused while it remains active — it must first be
    swapped (R4/T-024) to another downloaded model.

    :raises ModelIsActiveError: if ``model_id`` is the currently-active model.
    """
    if active_model_id(listing) == model_id:
        raise ModelIsActiveError(model_id)
