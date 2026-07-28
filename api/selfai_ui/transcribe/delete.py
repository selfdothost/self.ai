"""Transcribe-router model deletion request contract (cavekit-audio-transcribe-router R5).

A downloaded model no longer needed can be removed to reclaim storage. The
deletion itself is a single control-port command to the self-hosted STT backend
(the STT analog of self.llamolotl's ``/api/models/delete`` — see
``routers/llamolotl.py``): the backend removes the model files on its end, after
which the model drops out of the ``/api/models`` view and so out of the R1
downloaded-and-ready listing (R5 AC2). It may still appear as *pullable* if it is
a known catalog entry.

This module holds only the *pure* half of that operation — building the request
body — so the request contract is unit-testable without a running backend,
mirroring :func:`selfai_ui.transcribe.pull.build_cancel_payload`. The
active-model invariant gate that R5 defers to (R7 AC2 — the active model may not
be deleted while active) lives in :mod:`selfai_ui.transcribe.invariant`
(``assert_deletable``); the router calls that *before* issuing this delete.
"""


def build_delete_payload(model_id: str) -> dict:
    """Build the control-port delete request body for ``model_id`` (R5).

    Identifies the model to remove by the same ``id``/``name`` shape the pull and
    cancel commands use (see :func:`selfai_ui.transcribe.pull.build_pull_payload`),
    so the backend can key on whichever field it tracks the model under. Pure, so
    the delete request contract is testable independently of the router — the STT
    analog of self.llamolotl's ``/api/models/delete``.
    """
    return {"id": model_id, "name": model_id}
