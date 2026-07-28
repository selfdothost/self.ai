"""Unit tests for transcribe-router pull-progress normalization.

Covers cavekit-audio-transcribe-router R2 at the pure-function level:
  AC2 — an in-flight pull yields incremental ``pulling`` progress events.
  AC3 — a completed pull yields a terminal ``success`` event.
  AC4 — a failed pull yields a terminal ``error`` event, distinguishable from
        a success.
  AC5 — a truncated/failed pull never reports a model as downloaded-and-ready
        (this module has no path that does so; the terminal event is an error).

The router-level streaming/forwarding behaviour is covered by
``tests/routers/test_transcribe_pull.py``.
"""

import pytest

from selfai_ui.transcribe.pull import (
    PULL_PHASE_CANCELLED,
    PULL_PHASE_ERROR,
    PULL_PHASE_PULLING,
    PULL_PHASE_SUCCESS,
    build_cancel_payload,
    build_pull_payload,
    cancelled_event,
    error_event,
    incomplete_stream_event,
    is_cancelled,
    is_failure,
    is_success,
    is_terminal,
    normalize_pull_event,
)


@pytest.mark.tier0
def test_pull_payload_carries_id_and_name():
    assert build_pull_payload("whisper-large-v3") == {
        "id": "whisper-large-v3",
        "name": "whisper-large-v3",
    }


@pytest.mark.tier0
def test_in_flight_chunk_is_incremental_progress():
    """AC2: a mid-pull chunk normalizes to a non-terminal `pulling` event."""
    event = normalize_pull_event(
        {"status": "downloading", "completed": 250, "total": 1000},
        model_id="whisper-large-v3",
    )
    assert event["phase"] == PULL_PHASE_PULLING
    assert is_terminal(event) is False
    assert event["completed"] == 250
    assert event["total"] == 1000
    assert event["percent"] == 25.0
    assert event["id"] == "whisper-large-v3"


@pytest.mark.tier0
def test_completed_equal_total_alone_is_not_success():
    """A per-layer byte count reaching its total must NOT be read as success."""
    event = normalize_pull_event({"status": "downloading", "completed": 1000, "total": 1000})
    assert event["phase"] == PULL_PHASE_PULLING
    assert is_success(event) is False


@pytest.mark.tier0
def test_explicit_success_is_terminal_success():
    """AC3: an explicit success marker is a terminal `success` event."""
    event = normalize_pull_event({"status": "success"}, model_id="whisper-large-v3")
    assert event["phase"] == PULL_PHASE_SUCCESS
    assert is_success(event) is True
    assert is_terminal(event) is True
    assert event["message"] is None


@pytest.mark.tier0
def test_done_flag_is_success():
    event = normalize_pull_event({"done": True})
    assert event["phase"] == PULL_PHASE_SUCCESS


@pytest.mark.tier0
def test_error_field_is_terminal_error():
    """AC4: an error chunk is a terminal `error`, distinct from success."""
    event = normalize_pull_event({"error": "no space left on device"}, model_id="whisper-large-v3")
    assert event["phase"] == PULL_PHASE_ERROR
    assert is_failure(event) is True
    assert is_success(event) is False
    assert is_terminal(event) is True
    assert event["message"] == "no space left on device"


@pytest.mark.tier0
def test_error_status_text_is_error():
    event = normalize_pull_event({"status": "download failed"})
    assert event["phase"] == PULL_PHASE_ERROR
    assert event["message"] == "download failed"


@pytest.mark.tier0
def test_success_and_error_are_distinguishable():
    """AC4: success and error events never collide on the discriminator."""
    success = normalize_pull_event({"status": "success"})
    failure = normalize_pull_event({"error": "boom"})
    assert success["phase"] != failure["phase"]
    assert is_success(success) and not is_success(failure)
    assert is_failure(failure) and not is_failure(success)


@pytest.mark.tier0
def test_malformed_chunk_becomes_error_not_progress():
    """A non-dict chunk is an error, never silently treated as progress."""
    event = normalize_pull_event("garbage", model_id="whisper-small")
    assert event["phase"] == PULL_PHASE_ERROR
    assert event["id"] == "whisper-small"
    assert "malformed" in event["message"]


@pytest.mark.tier0
def test_fractional_progress_scaled_to_percent():
    event = normalize_pull_event({"status": "downloading", "progress": 0.4})
    assert event["percent"] == 40.0


@pytest.mark.tier0
def test_percent_field_passthrough_and_clamp():
    assert normalize_pull_event({"status": "downloading", "percent": 42})["percent"] == 42.0
    assert normalize_pull_event({"status": "downloading", "percent": 250})["percent"] == 100.0
    assert normalize_pull_event({"status": "downloading", "percent": -5})["percent"] == 0.0


@pytest.mark.tier0
def test_incomplete_stream_event_is_error():
    """AC5: a stream ending with no terminal event is treated as a failure."""
    event = incomplete_stream_event("whisper-large-v3")
    assert event["phase"] == PULL_PHASE_ERROR
    assert is_failure(event) is True
    assert event["id"] == "whisper-large-v3"


@pytest.mark.tier0
def test_error_event_builder():
    event = error_event("m", "upstream down")
    assert event["phase"] == PULL_PHASE_ERROR
    assert event["message"] == "upstream down"
    assert event["id"] == "m"


# ---------------------------------------------------------------------------
# R3: Pull cancellation (terminal `cancelled` phase, distinct from `error`)
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_cancel_payload_carries_id_and_name():
    assert build_cancel_payload("whisper-large-v3") == {
        "id": "whisper-large-v3",
        "name": "whisper-large-v3",
    }


@pytest.mark.tier0
def test_cancelled_status_is_terminal_cancelled():
    """R3 AC1: a `cancelled` status chunk is a terminal `cancelled` event."""
    event = normalize_pull_event({"status": "cancelled"}, model_id="whisper-large-v3")
    assert event["phase"] == PULL_PHASE_CANCELLED
    assert is_cancelled(event) is True
    assert is_terminal(event) is True


@pytest.mark.tier0
def test_cancelled_single_l_spelling_also_recognized():
    """The American spelling ('canceled') is recognized too."""
    event = normalize_pull_event({"status": "canceled"})
    assert event["phase"] == PULL_PHASE_CANCELLED


@pytest.mark.tier0
def test_cancelled_flag_is_terminal_cancelled():
    """An explicit boolean cancel flag is a terminal `cancelled` event."""
    assert normalize_pull_event({"cancelled": True})["phase"] == PULL_PHASE_CANCELLED
    assert normalize_pull_event({"canceled": True})["phase"] == PULL_PHASE_CANCELLED


@pytest.mark.tier0
def test_cancelled_is_not_success_and_not_failure():
    """R3 AC2: a cancel is neither a success nor a plain failure — its own phase,
    so a cancelled pull is never read as downloaded-and-ready."""
    event = normalize_pull_event({"status": "pull cancelled by user"})
    assert event["phase"] == PULL_PHASE_CANCELLED
    assert is_success(event) is False
    assert is_failure(event) is False


@pytest.mark.tier0
def test_explicit_error_field_wins_over_cancel_text():
    """A concrete hard-error field is taken at its word over a cancel hint."""
    event = normalize_pull_event({"error": "disk failure", "status": "cancelled"})
    assert event["phase"] == PULL_PHASE_ERROR
    assert event["message"] == "disk failure"


@pytest.mark.tier0
def test_cancelled_event_builder():
    event = cancelled_event("whisper-small")
    assert event["phase"] == PULL_PHASE_CANCELLED
    assert is_cancelled(event) is True
    assert is_terminal(event) is True
    assert is_success(event) is False
    assert event["id"] == "whisper-small"


@pytest.mark.tier0
def test_cancelled_error_success_all_distinct():
    """The three terminal phases never collide on the discriminator."""
    cancelled = normalize_pull_event({"status": "cancelled"})
    failure = normalize_pull_event({"error": "boom"})
    success = normalize_pull_event({"status": "success"})
    phases = {cancelled["phase"], failure["phase"], success["phase"]}
    assert len(phases) == 3


@pytest.mark.tier0
def test_id_echoed_from_chunk_over_hint():
    """A backend-echoed id in the chunk wins over the caller's hint."""
    event = normalize_pull_event({"name": "whisper-medium", "status": "downloading"}, model_id="hint")
    assert event["id"] == "whisper-medium"
