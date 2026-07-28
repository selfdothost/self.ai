"""Transcribe-router model pull progress (cavekit-audio-transcribe-router R2).

Pure, backend-agnostic normalization of a self-hosted STT backend's model-pull
progress stream into a single, self-describing event contract. The transcribe
router streams a pull by proxying the backend control port's
``/api/models/pull`` NDJSON stream (the STT analog of self.llamolotl's
``/api/models/pull`` — see ``routers/llamolotl.py``); every raw chunk is passed
through :func:`normalize_pull_event` so each emitted event carries an explicit
``phase`` discriminator rather than an opaque backend-specific shape.

The phases partition every event:

* ``pulling``  — an in-flight progress update (bytes/percent so far). There may
  be many of these; this is what makes progress *incremental* rather than a
  single terminal success/failure (R2 AC2).
* ``success``  — the terminal event of a completed pull. Only after this does
  the model become downloaded-and-ready per the R1 listing (R2 AC3).
* ``error``    — a terminal failure event, unambiguously distinguishable from a
  ``success`` (R2 AC4). A stream that ends without any terminal event is treated
  as an ``error`` (see :func:`incomplete_stream_event`) so a truncated or
  dropped pull is never mistaken for a completed one.
* ``cancelled`` — the terminal event of a pull deliberately cancelled before it
  completed (R3). Distinct from ``error``: a cancel is an intentional caller
  action, not a failure, and — like ``error`` but unlike ``success`` — it never
  leaves the model downloaded-and-ready (R3 AC2). Kept as its own phase so a
  consumer can show "cancelled" separately from "failed" and never confuse
  either with completion.

This module intentionally holds *no* I/O and *no* heavy dependencies (stdlib
only), so its classification logic is unit-testable without a running backend —
the same split T-020 used for ``listing.py``.

Failure containment (R2 AC5): this module never reports a model as
downloaded-and-ready. That set is owned solely by the R1 listing
(``listing.py``), which reflects only what the backend actually holds on its
``/api/models`` view. A failed or truncated pull therefore cannot leak a partial
model into the ready set — there is no optimistic write-through here to leak it.
"""

from typing import Any, Optional

# Phase discriminator values. Plain strings (not an Enum) so the streamed JSON
# contract stays a stable, self-describing literal for every consumer.
PULL_PHASE_PULLING = "pulling"
PULL_PHASE_SUCCESS = "success"
PULL_PHASE_ERROR = "error"
PULL_PHASE_CANCELLED = "cancelled"

# Substrings that mark a status/phase string as terminal-success, terminal-
# error, or terminal-cancelled. Matched case-insensitively against whatever
# text the backend reports. ``cancel`` as a substring covers "cancel",
# "canceled", "cancelling", and "cancelled" (both spellings).
_SUCCESS_TOKENS = ("success", "complete", "completed", "done", "ready")
_ERROR_TOKENS = ("error", "fail", "failed", "failure", "aborted")
_CANCEL_TOKENS = ("cancel",)


def build_pull_payload(model_id: str) -> dict:
    """Build the control-port pull request body for ``model_id``.

    Sends the id under both ``id`` and ``name`` so the backend can key on
    whichever it expects (the listing already matches on either — see
    ``listing._entry_id``). Kept as a pure function so the request contract is
    testable independently of the router.
    """
    return {"id": model_id, "name": model_id}


def build_cancel_payload(model_id: str) -> dict:
    """Build the control-port cancel request body for ``model_id`` (R3).

    Identifies the in-flight pull to stop by the same ``id``/``name`` shape the
    pull command uses (see :func:`build_pull_payload`), so the backend can key
    on whichever field it tracks the pull under. Pure, so the cancel request
    contract is testable independently of the router — the STT analog of
    self.llamolotl's ``/api/models/pull/cancel``.
    """
    return {"id": model_id, "name": model_id}


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _status_text(raw: dict) -> Optional[str]:
    """Extract a human-readable status/phase string from a raw chunk."""
    for key in ("status", "phase", "state", "message"):
        val = raw.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def _percent(raw: dict, completed: Optional[int], total: Optional[int]) -> Optional[float]:
    """Resolve a 0-100 percentage from an explicit field or completed/total.

    Accepts an explicit ``percent`` (0-100) or ``progress`` (0-1 fraction, or
    0-100 if it exceeds 1), falling back to ``completed / total``. Clamped to
    [0, 100]. Returns None when nothing usable is present.
    """
    explicit = _as_float(raw.get("percent"))
    if explicit is None:
        progress = _as_float(raw.get("progress"))
        if progress is not None:
            explicit = progress * 100.0 if progress <= 1.0 else progress
    if explicit is None and completed is not None and total is not None and total > 0:
        explicit = (completed / total) * 100.0
    if explicit is None:
        return None
    if explicit < 0:
        return 0.0
    if explicit > 100:
        return 100.0
    return round(explicit, 2)


def _classify_phase(raw: dict, status: Optional[str]) -> tuple[str, Optional[str]]:
    """Decide the phase for a raw chunk and, for errors/cancels, its message.

    Precedence is error > cancelled > success > pulling. Success is only ever
    inferred from an *explicit* success/done marker — never from
    ``completed >= total``, since a per-layer byte count reaching its total does
    not mean the whole pull is finished. An explicit hard-``error`` field still
    wins over a cancel signal (a backend that reports a concrete failure is
    taken at its word); a plain ``cancelled`` status/flag, which matches none of
    the error tokens, is classified as its own terminal ``cancelled`` phase
    rather than silently falling through to ``pulling``.
    """
    # 1) Explicit error field wins outright.
    err = raw.get("error")
    if isinstance(err, str) and err:
        return PULL_PHASE_ERROR, err
    if err is True:
        return PULL_PHASE_ERROR, (status or "pull failed")

    lowered = status.lower() if status else ""

    # 2) Explicit cancellation flag or status text (R3): an intentional stop,
    #    distinct from a failure. Checked before the success/pulling fallbacks
    #    so a "cancelled" status is never mistaken for ongoing progress.
    if raw.get("cancelled") is True or raw.get("canceled") is True:
        return PULL_PHASE_CANCELLED, (status or "pull cancelled")
    if any(tok in lowered for tok in _CANCEL_TOKENS):
        return PULL_PHASE_CANCELLED, status or "pull cancelled"

    # 3) Status/phase text that names an error.
    if any(tok in lowered for tok in _ERROR_TOKENS):
        return PULL_PHASE_ERROR, status or "pull failed"

    # 4) Explicit success/done markers.
    if raw.get("done") is True or raw.get("success") is True:
        return PULL_PHASE_SUCCESS, None
    if any(tok in lowered for tok in _SUCCESS_TOKENS):
        return PULL_PHASE_SUCCESS, None

    # 5) Otherwise it is an in-flight progress update.
    return PULL_PHASE_PULLING, None


def normalize_pull_event(raw: Any, model_id: Optional[str] = None) -> dict:
    """Normalize one raw backend pull chunk into the stable event contract.

    Returns a dict with:

    * ``id``        — the model id (echoed from ``raw`` or the caller's hint).
    * ``phase``     — ``pulling`` | ``success`` | ``error`` (the discriminator).
    * ``status``    — the backend's human-readable status text, when present.
    * ``completed`` — bytes/units downloaded so far, when reported.
    * ``total``     — total bytes/units, when reported.
    * ``percent``   — 0-100 progress, when derivable.
    * ``message``   — the failure message when ``phase == "error"`` or the
      cancellation message when ``phase == "cancelled"``, else None.

    A non-dict chunk (or one that cannot be parsed) is reported as an ``error``
    rather than raising, so a malformed backend line cannot be silently taken
    for progress or success.
    """
    if not isinstance(raw, dict):
        return {
            "id": model_id,
            "phase": PULL_PHASE_ERROR,
            "status": None,
            "completed": None,
            "total": None,
            "percent": None,
            "message": "malformed pull progress from backend",
        }

    status = _status_text(raw)
    completed = _as_int(raw.get("completed"))
    total = _as_int(raw.get("total"))
    phase, message = _classify_phase(raw, status)

    resolved_id = model_id
    for key in ("id", "name", "model"):
        val = raw.get(key)
        if isinstance(val, str) and val:
            resolved_id = val
            break

    return {
        "id": resolved_id,
        "phase": phase,
        "status": status,
        "completed": completed,
        "total": total,
        "percent": _percent(raw, completed, total),
        "message": message,
    }


def error_event(model_id: Optional[str], message: str) -> dict:
    """Build a terminal ``error`` event (e.g. for an upstream HTTP failure)."""
    return {
        "id": model_id,
        "phase": PULL_PHASE_ERROR,
        "status": None,
        "completed": None,
        "total": None,
        "percent": None,
        "message": message,
    }


def cancelled_event(model_id: Optional[str], message: str = "pull cancelled") -> dict:
    """Build a terminal ``cancelled`` event (R3).

    Used to synthesize a terminal cancellation on the pull stream — distinct
    from :func:`error_event`. Like an error it is terminal and never marks the
    model downloaded-and-ready (R3 AC2), but it names an intentional stop rather
    than a failure so a consumer can present the two differently.
    """
    return {
        "id": model_id,
        "phase": PULL_PHASE_CANCELLED,
        "status": None,
        "completed": None,
        "total": None,
        "percent": None,
        "message": message,
    }


def incomplete_stream_event(model_id: Optional[str]) -> dict:
    """Terminal ``error`` for a stream that ended without any terminal event.

    A pull whose progress stream is truncated (backend crash, dropped
    connection) has *not* succeeded; surfacing it as an error keeps a partial
    download from ever being presented as downloaded-and-ready (R2 AC5).
    """
    return error_event(model_id, "pull ended before completion (no terminal status received)")


def is_terminal(event: dict) -> bool:
    """True if this event ends the pull (success, error, or cancelled)."""
    return event.get("phase") in (
        PULL_PHASE_SUCCESS,
        PULL_PHASE_ERROR,
        PULL_PHASE_CANCELLED,
    )


def is_success(event: dict) -> bool:
    return event.get("phase") == PULL_PHASE_SUCCESS


def is_failure(event: dict) -> bool:
    return event.get("phase") == PULL_PHASE_ERROR


def is_cancelled(event: dict) -> bool:
    return event.get("phase") == PULL_PHASE_CANCELLED
