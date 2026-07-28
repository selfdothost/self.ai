"""Unit tests for the transcribe-router active-model swap gate.

Covers cavekit-audio-transcribe-router R4 at the pure-function level:
  AC3 — only a downloaded-and-ready model (R1) can be made active.

The "no restart" (AC1) and "served by the new model after swap" (AC2) criteria
are structural — a live control-port ``load`` command, no process restart — and
are exercised at the router/endpoint level in ``tests/routers/test_transcribe``.
"""

import pytest

from selfai_ui.transcribe.swap import (
    ModelNotDownloadedError,
    downloaded_ready_ids,
    validate_swap_target,
)

# Two raw control-port views: one model downloaded-and-ready, one merely pullable.
DOWNLOADED_RAW = {"data": [{"id": "whisper-small", "status": {"value": "loaded"}}]}
CATALOG_RAW = [
    {"id": "whisper-small"},  # already downloaded
    {"id": "whisper-large-v3", "description": "Large multilingual"},  # pullable only
]


@pytest.mark.tier0
def test_downloaded_ready_ids_only_counts_downloaded():
    """The eligible set is exactly the downloaded-and-ready models."""
    from selfai_ui.transcribe.listing import build_model_listing

    listing = build_model_listing(DOWNLOADED_RAW, CATALOG_RAW)
    assert downloaded_ready_ids(listing) == {"whisper-small"}


@pytest.mark.tier0
def test_validate_accepts_a_downloaded_model():
    """AC3: a downloaded-and-ready model is a valid swap target."""
    assert validate_swap_target(DOWNLOADED_RAW, CATALOG_RAW, "whisper-small") == "whisper-small"


@pytest.mark.tier0
def test_validate_rejects_a_pullable_only_model():
    """AC3: a merely-pullable model cannot be made active."""
    with pytest.raises(ModelNotDownloadedError):
        validate_swap_target(DOWNLOADED_RAW, CATALOG_RAW, "whisper-large-v3")


@pytest.mark.tier0
def test_validate_rejects_an_unknown_model():
    """AC3: a model in neither view cannot be made active."""
    with pytest.raises(ModelNotDownloadedError):
        validate_swap_target(DOWNLOADED_RAW, CATALOG_RAW, "does-not-exist")


@pytest.mark.tier0
def test_validate_rejects_when_nothing_is_downloaded():
    """With an empty backend, no swap target is valid."""
    with pytest.raises(ModelNotDownloadedError):
        validate_swap_target({"data": []}, [], "whisper-small")


@pytest.mark.tier0
def test_downloaded_ready_ids_ignores_malformed_entries():
    """A malformed listing entry never enters the swap-eligible set."""
    listing = {
        "data": [
            {"id": "good", "downloaded": True, "availability": "downloaded"},
            {"id": "", "downloaded": True, "availability": "downloaded"},  # empty id
            {"downloaded": True, "availability": "downloaded"},  # no id
            {"id": "pullable", "downloaded": False, "availability": "pullable"},
            "not-a-dict",
        ]
    }
    assert downloaded_ready_ids(listing) == {"good"}
