"""Pure unit tests for the transcribe-router delete request contract (R5).

Covers :func:`selfai_ui.transcribe.delete.build_delete_payload` and the
active-model deletion gate it composes with
(:func:`selfai_ui.transcribe.invariant.assert_deletable`) — no backend or router
required, mirroring ``test_transcribe_pull.py`` / ``test_transcribe_invariant.py``.
"""

import pytest

from selfai_ui.transcribe.delete import build_delete_payload
from selfai_ui.transcribe.invariant import ModelIsActiveError, assert_deletable
from selfai_ui.transcribe.listing import build_model_listing


@pytest.mark.tier1
def test_build_delete_payload_carries_id_and_name():
    """The delete body identifies the model by both id and name, like pull/cancel,
    so the backend can key on whichever field it tracks the model under."""
    assert build_delete_payload("whisper-small") == {
        "id": "whisper-small",
        "name": "whisper-small",
    }


@pytest.mark.tier1
def test_assert_deletable_rejects_active_model():
    """R5 AC4 / R7 AC2: the currently-active (loaded) model is not deletable."""
    listing = build_model_listing(
        [{"id": "whisper-small", "status": {"value": "loaded"}}],
        [{"id": "whisper-small"}],
    )
    with pytest.raises(ModelIsActiveError) as excinfo:
        assert_deletable(listing, "whisper-small")
    assert excinfo.value.model_id == "whisper-small"


@pytest.mark.tier1
def test_assert_deletable_allows_non_active_downloaded_model():
    """R5 AC1: a downloaded model that is not the active one is deletable."""
    listing = build_model_listing(
        [
            {"id": "whisper-small", "status": {"value": "loaded"}},
            {"id": "whisper-large-v3", "status": {"value": "unloaded"}},
        ],
        [{"id": "whisper-small"}, {"id": "whisper-large-v3"}],
    )
    # Non-active downloaded model: no exception.
    assert_deletable(listing, "whisper-large-v3")
