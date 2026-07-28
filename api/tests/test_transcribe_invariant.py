"""Unit tests for the transcribe-router active-model invariant (T-025).

Covers cavekit-audio-transcribe-router R7 at the pure-function level:
  AC1 — with >=1 downloaded, exactly one downloaded model reported active.
  AC2 — deleting the active model is refused while it is active.
  AC3 — with 0 downloaded, an explicit "no active model" state.
  AC4 — no resulting listing leaves downloaded-but-none-active unflagged.

Built on synthetic listings (the T-020 listing shape) plus real listings from
``build_model_listing``, so the invariant is exercised against exactly what the
rest of the domain produces. Deletion (T-026) is a later tier, so AC2 is tested
against the guard T-026 will call, not a live delete endpoint.
"""

import pytest

from selfai_ui.transcribe.invariant import (
    ACTIVE_STATUSES,
    ActiveModelInvariantError,
    ModelIsActiveError,
    active_model_id,
    assert_deletable,
    assert_operation_preserves_invariant,
    check_active_model_invariant,
    downloaded_ids_in_order,
    establish_target,
    has_active_model,
    inspect_active_model,
    mark_active_model,
    reported_active_ids,
)
from selfai_ui.transcribe.listing import build_model_listing


def _entry(model_id, downloaded=True, status=None, availability=None):
    if availability is None:
        availability = "downloaded" if downloaded else "pullable"
    return {
        "id": model_id,
        "name": model_id,
        "downloaded": downloaded,
        "availability": availability,
        "status": status,
    }


def _listing(*entries):
    return {"data": list(entries)}


# --- AC1: exactly one active when >=1 downloaded -------------------------------


@pytest.mark.tier2
def test_single_downloaded_loaded_model_is_active():
    listing = _listing(_entry("whisper-small", status="loaded"))
    assert active_model_id(listing) == "whisper-small"
    assert has_active_model(listing) is True


@pytest.mark.tier2
def test_exactly_one_of_several_downloaded_is_active():
    listing = _listing(
        _entry("whisper-small", status="unloaded"),
        _entry("whisper-medium", status="loaded"),
        _entry("whisper-large", status="unloaded"),
        _entry("whisper-tiny", downloaded=False),  # pullable, ignored
    )
    state = inspect_active_model(listing)
    assert state.active_id == "whisper-medium"
    assert state.reported_active_ids == ("whisper-medium",)
    assert state.ok is True
    assert state.anomaly is None


@pytest.mark.tier2
def test_active_status_from_activate_endpoint_is_recognized():
    # T-024's activate endpoint returns status "active"; it must count as active.
    listing = _listing(_entry("whisper-small", status="active"))
    assert active_model_id(listing) == "whisper-small"


@pytest.mark.tier2
def test_active_status_matching_is_case_and_whitespace_insensitive():
    listing = _listing(_entry("whisper-small", status="  Loaded "))
    assert active_model_id(listing) == "whisper-small"


@pytest.mark.tier2
def test_real_build_model_listing_resolves_active_model():
    downloaded_raw = {
        "data": [
            {"id": "whisper-small", "status": {"value": "loaded"}},
            {"id": "whisper-medium", "status": {"value": "unloaded"}},
        ]
    }
    catalog_raw = [{"id": "whisper-small"}, {"id": "whisper-large-v3"}]
    listing = build_model_listing(downloaded_raw, catalog_raw)
    assert active_model_id(listing) == "whisper-small"
    check_active_model_invariant(listing)  # healthy -> no raise


# --- AC3: zero downloaded -> explicit no-active state --------------------------


@pytest.mark.tier2
def test_no_models_downloaded_reports_no_active_model():
    listing = _listing()
    state = inspect_active_model(listing)
    assert state.active_id is None
    assert state.has_active is False
    assert state.has_downloaded is False
    assert state.ok is True  # legitimate empty state, not a violation
    check_active_model_invariant(listing)  # must NOT raise


@pytest.mark.tier2
def test_only_pullable_models_reports_no_active_model():
    # Catalog entries exist but nothing is downloaded -> still no active model.
    listing = _listing(
        _entry("whisper-small", downloaded=False),
        _entry("whisper-large", downloaded=False),
    )
    state = inspect_active_model(listing)
    assert state.active_id is None
    assert state.ok is True
    check_active_model_invariant(listing)


@pytest.mark.tier2
def test_stale_status_on_a_pullable_entry_is_not_reported_active():
    # A pullable entry must never be reported active even if it carries a status.
    listing = _listing(_entry("ghost", downloaded=False, availability="pullable", status="loaded"))
    assert active_model_id(listing) is None


# --- AC4: no state leaves downloaded-but-none-active unflagged -----------------


@pytest.mark.tier2
def test_downloaded_but_none_active_is_flagged_as_violation():
    listing = _listing(
        _entry("whisper-small", status="unloaded"),
        _entry("whisper-medium", status="unloaded"),
    )
    state = inspect_active_model(listing)
    assert state.active_id is None
    assert state.has_downloaded is True
    assert state.ok is False
    assert state.anomaly is not None
    with pytest.raises(ActiveModelInvariantError):
        check_active_model_invariant(listing)


@pytest.mark.tier2
def test_downloaded_with_no_status_at_all_is_a_violation():
    # A fresh pull (T-021) that doesn't activate leaves status None -> violation.
    listing = _listing(_entry("whisper-small", status=None))
    state = inspect_active_model(listing)
    assert state.active_id is None
    assert state.ok is False
    with pytest.raises(ActiveModelInvariantError):
        check_active_model_invariant(listing)


@pytest.mark.tier2
def test_establish_target_names_first_downloaded_to_repair_violation():
    listing = _listing(
        _entry("whisper-small", status=None),
        _entry("whisper-medium", status=None),
    )
    assert establish_target(listing) == "whisper-small"


@pytest.mark.tier2
def test_establish_target_is_none_when_already_active():
    listing = _listing(_entry("whisper-small", status="loaded"))
    assert establish_target(listing) is None


@pytest.mark.tier2
def test_establish_target_is_none_when_nothing_downloaded():
    assert establish_target(_listing()) is None


@pytest.mark.tier2
def test_multiple_active_is_anomaly_picks_first_deterministically():
    listing = _listing(
        _entry("whisper-small", status="loaded"),
        _entry("whisper-medium", status="loaded"),
    )
    state = inspect_active_model(listing)
    assert state.active_id == "whisper-small"  # first in listing order
    assert state.reported_active_ids == ("whisper-small", "whisper-medium")
    assert state.ok is False
    assert state.anomaly is not None
    with pytest.raises(ActiveModelInvariantError):
        check_active_model_invariant(listing)


@pytest.mark.tier2
def test_assert_operation_preserves_invariant_passes_on_healthy_after():
    before = _listing()  # fresh deploy, no active model (legit)
    after = _listing(_entry("whisper-small", status="loaded"))  # first pull + activate
    assert_operation_preserves_invariant(before, after)  # no raise


@pytest.mark.tier2
def test_assert_operation_preserves_invariant_catches_violating_after():
    before = _listing(_entry("whisper-small", status="loaded"))
    # Hypothetical op leaves a downloaded model but drops all active status.
    after = _listing(_entry("whisper-small", status="unloaded"))
    with pytest.raises(ActiveModelInvariantError):
        assert_operation_preserves_invariant(before, after)


@pytest.mark.tier2
def test_swap_result_preserves_invariant():
    # Model of the swap: before whisper-small active, after whisper-medium active.
    before = _listing(
        _entry("whisper-small", status="loaded"),
        _entry("whisper-medium", status="unloaded"),
    )
    after = _listing(
        _entry("whisper-small", status="unloaded"),
        _entry("whisper-medium", status="loaded"),
    )
    assert_operation_preserves_invariant(before, after)
    assert active_model_id(after) == "whisper-medium"


# --- AC2: deletion of the active model is refused -----------------------------


@pytest.mark.tier2
def test_deleting_the_active_model_is_refused():
    listing = _listing(
        _entry("whisper-small", status="loaded"),
        _entry("whisper-medium", status="unloaded"),
    )
    with pytest.raises(ModelIsActiveError) as exc:
        assert_deletable(listing, "whisper-small")
    assert exc.value.model_id == "whisper-small"


@pytest.mark.tier2
def test_deleting_a_non_active_downloaded_model_is_allowed():
    listing = _listing(
        _entry("whisper-small", status="loaded"),
        _entry("whisper-medium", status="unloaded"),
    )
    assert_deletable(listing, "whisper-medium")  # no raise


@pytest.mark.tier2
def test_deletable_after_swapping_active_away():
    # AC2 flow: swap active to whisper-medium, then whisper-small becomes deletable.
    after_swap = _listing(
        _entry("whisper-small", status="unloaded"),
        _entry("whisper-medium", status="loaded"),
    )
    assert_deletable(after_swap, "whisper-small")  # now allowed
    with pytest.raises(ModelIsActiveError):
        assert_deletable(after_swap, "whisper-medium")  # the new active is protected


@pytest.mark.tier2
def test_deleting_a_pullable_or_unknown_model_is_not_active_blocked():
    listing = _listing(
        _entry("whisper-small", status="loaded"),
        _entry("whisper-large", downloaded=False),
    )
    assert_deletable(listing, "whisper-large")  # pullable, not active
    assert_deletable(listing, "does-not-exist")  # unknown, not active


# --- helper-level guards ------------------------------------------------------


@pytest.mark.tier2
def test_downloaded_ids_in_order_preserves_order_and_ignores_malformed():
    listing = {
        "data": [
            _entry("a", status="unloaded"),
            {"id": "", "downloaded": True, "availability": "downloaded"},
            {"downloaded": True, "availability": "downloaded"},  # no id
            _entry("b", downloaded=False),  # pullable
            "not-a-dict",
            _entry("c", status="loaded"),
        ]
    }
    assert downloaded_ids_in_order(listing) == ["a", "c"]


@pytest.mark.tier2
def test_reported_active_ids_only_counts_active_downloaded():
    listing = _listing(
        _entry("a", status="loaded"),
        _entry("b", status="active"),
        _entry("c", status="unloaded"),
        _entry("d", downloaded=False, availability="pullable", status="loaded"),
    )
    assert reported_active_ids(listing) == ["a", "b"]


@pytest.mark.tier2
def test_active_statuses_constant_shape():
    assert "loaded" in ACTIVE_STATUSES
    assert "active" in ACTIVE_STATUSES


# --- mark_active_model: the picker's active indicator overlay (T-030) ---------
#
# cavekit-audio-transcribe-picker R3 at the pure-function level:
#   AC1 — the currently active (resident) model is marked in the list.
#   AC2 — the active marker is distinct from downloaded-and-ready (active is a
#         strict subset of downloaded; a pullable model is never active).
#   AC3 — when the active model changes, the marker moves on a fresh listing.


@pytest.mark.tier0
def test_mark_active_flags_only_the_resident_model():
    """AC1: exactly the loaded downloaded model is marked ``active``."""
    listing = _listing(
        _entry("whisper-small", status="unloaded"),
        _entry("whisper-medium", status="loaded"),
        _entry("whisper-large", status="unloaded"),
    )
    marked = mark_active_model(listing)
    by_id = {e["id"]: e for e in marked["data"]}
    assert by_id["whisper-medium"]["active"] is True
    assert by_id["whisper-small"]["active"] is False
    assert by_id["whisper-large"]["active"] is False


@pytest.mark.tier0
def test_mark_active_is_distinct_from_downloaded():
    """AC2: ``active`` is strictly narrower than ``downloaded`` — a downloaded but
    unloaded model, and a merely-pullable one, are both non-active."""
    listing = _listing(
        _entry("whisper-small", status="loaded"),  # downloaded AND active
        _entry("whisper-medium", status="unloaded"),  # downloaded, NOT active
        _entry("whisper-large", downloaded=False),  # pullable, never active
    )
    by_id = {e["id"]: e for e in mark_active_model(listing)["data"]}

    # The active one is downloaded-and-active — more than just downloaded.
    assert by_id["whisper-small"]["downloaded"] is True
    assert by_id["whisper-small"]["active"] is True
    # A downloaded-but-unloaded model is downloaded yet NOT active — the marker
    # says something the downloaded flag does not.
    assert by_id["whisper-medium"]["downloaded"] is True
    assert by_id["whisper-medium"]["active"] is False
    # A pullable model is neither downloaded nor active.
    assert by_id["whisper-large"]["downloaded"] is False
    assert by_id["whisper-large"]["active"] is False


@pytest.mark.tier0
def test_mark_active_pullable_never_active_even_if_status_loaded():
    """AC2: a merely-pullable entry is never marked active, even if it carries a
    stray ``loaded`` status — active tracks the downloaded resident model only."""
    listing = _listing(
        _entry("whisper-small", status="loaded"),
        _entry("whisper-large", downloaded=False, availability="pullable", status="loaded"),
    )
    by_id = {e["id"]: e for e in mark_active_model(listing)["data"]}
    assert by_id["whisper-small"]["active"] is True
    assert by_id["whisper-large"]["active"] is False


@pytest.mark.tier0
def test_mark_active_marker_moves_when_active_model_changes():
    """AC3: after the backend's active model changes, a fresh listing moves the
    marker to the new active model (the overlay holds no state of its own)."""
    before = _listing(
        _entry("whisper-small", status="loaded"),
        _entry("whisper-medium", status="unloaded"),
    )
    after = _listing(
        _entry("whisper-small", status="unloaded"),
        _entry("whisper-medium", status="loaded"),
    )
    before_by_id = {e["id"]: e for e in mark_active_model(before)["data"]}
    after_by_id = {e["id"]: e for e in mark_active_model(after)["data"]}

    assert before_by_id["whisper-small"]["active"] is True
    assert before_by_id["whisper-medium"]["active"] is False
    # Marker moved.
    assert after_by_id["whisper-small"]["active"] is False
    assert after_by_id["whisper-medium"]["active"] is True


@pytest.mark.tier0
def test_mark_active_no_downloaded_marks_nothing():
    """No models downloaded (legitimate no-active state): every entry non-active."""
    listing = _listing(
        _entry("whisper-small", downloaded=False),
        _entry("whisper-large", downloaded=False),
    )
    assert all(e["active"] is False for e in mark_active_model(listing)["data"])


@pytest.mark.tier0
def test_mark_active_anomalous_none_active_marks_nothing():
    """Downloaded but none loaded (an anomaly): no entry is marked active rather
    than guessing one — the marker only ever tracks a genuinely-resident model."""
    listing = _listing(
        _entry("whisper-small", status="unloaded"),
        _entry("whisper-medium", status="unloaded"),
    )
    assert all(e["active"] is False for e in mark_active_model(listing)["data"])


@pytest.mark.tier0
def test_mark_active_is_pure_and_tolerates_non_dict_entries():
    """The overlay copies entries (no in-place mutation) and passes non-dict rows
    through untouched."""
    original_entry = _entry("whisper-small", status="loaded")
    listing = {"data": [original_entry, "not-a-dict"]}
    marked = mark_active_model(listing)

    # Source entry not mutated.
    assert "active" not in original_entry
    # Copied, flagged entry in the result.
    assert marked["data"][0]["active"] is True
    # Non-dict row carried through.
    assert marked["data"][1] == "not-a-dict"


@pytest.mark.tier0
def test_mark_active_over_real_build_model_listing():
    """End-to-end over a real ``build_model_listing`` result: the loaded model is
    marked active, the pullable catalog-only one is not."""
    downloaded_raw = {"data": [{"id": "whisper-small", "status": {"value": "loaded"}}]}
    catalog_raw = [{"id": "whisper-small"}, {"id": "whisper-large-v3"}]
    listing = build_model_listing(downloaded_raw, catalog_raw)
    by_id = {e["id"]: e for e in mark_active_model(listing)["data"]}
    assert by_id["whisper-small"]["active"] is True
    assert by_id["whisper-large-v3"]["active"] is False
