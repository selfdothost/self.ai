"""Active-model swap validation (cavekit-audio-transcribe-router R4).

Which downloaded model actually serves transcription requests can change
without a full service restart. The swap itself is a single control-port call
to the self-hosted STT backend (the STT analog of self.llamolotl's
``/models/load`` — see ``routers/llamolotl.py``): the backend loads the target
model and, being a single-active-model router, drops the previously active one.
No UI-server process restart is involved — activation is a live control-port
mutation.

This module holds the *pure* half of that operation: deciding whether a swap
target is a legitimate one. R4's third criterion is that **only** a
downloaded-and-ready model (R1) may be made active, so the swap is gated on
exactly the R1 listing T-020 already builds — this reuses
:func:`build_model_listing` rather than re-deriving "downloaded" from the raw
backend views, keeping the swap gate and the listing contract in lockstep.
"""

from typing import Any

from selfai_ui.transcribe.listing import AVAILABILITY_DOWNLOADED, build_model_listing


class ModelNotDownloadedError(Exception):
    """A swap targeted a model that is not downloaded-and-ready (R4 AC3).

    Raised so the router can translate it into a client-facing rejection: a
    pullable-only or entirely-unknown model may not be made active.
    """

    def __init__(self, model_id: str):
        self.model_id = model_id
        super().__init__(
            f"model {model_id!r} is not downloaded and ready to serve; "
            "only a downloaded model can be made active"
        )


def downloaded_ready_ids(listing: dict) -> set[str]:
    """Return the set of downloaded-and-ready model ids in an R1 listing.

    A model qualifies only when it is *both* flagged ``downloaded`` and carries
    the ``downloaded`` availability discriminator — the two are redundant on
    purpose in :class:`TranscribeModelEntry`, and requiring both here means a
    malformed entry can never sneak into the swap-eligible set.
    """
    ids: set[str] = set()
    for entry in listing.get("data", []):
        if not isinstance(entry, dict):
            continue
        if entry.get("downloaded") is True and entry.get("availability") == AVAILABILITY_DOWNLOADED:
            model_id = entry.get("id")
            if isinstance(model_id, str) and model_id:
                ids.add(model_id)
    return ids


def validate_swap_target(downloaded_raw: Any, catalog_raw: Any, target_id: str) -> str:
    """Validate a swap target against the R1 downloaded-and-ready set.

    Builds the same listing T-020 exposes from the two raw control-port views
    and confirms ``target_id`` is among the downloaded-and-ready models.

    :returns: the validated ``target_id`` on success.
    :raises ModelNotDownloadedError: if the target is not downloaded-and-ready.
    """
    listing = build_model_listing(downloaded_raw, catalog_raw)
    if target_id not in downloaded_ready_ids(listing):
        raise ModelNotDownloadedError(target_id)
    return target_id
