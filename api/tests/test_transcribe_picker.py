"""Pure-logic tests for the transcribe-picker empty/fallback resolver (R5, T-032).

``resolve_selectable_state`` composes T-031's ``filter_enabled_models`` with
T-025's ``inspect_active_model`` over a T-020 listing to pick one of three
picker states — enabled / fallback / unavailable — without touching config or
the network. These tests exercise that policy directly.
"""

import pytest

from selfai_ui.transcribe.listing import build_model_listing
from selfai_ui.transcribe.picker import (
    SELECTABLE_MODE_ENABLED,
    SELECTABLE_MODE_FALLBACK,
    SELECTABLE_MODE_UNAVAILABLE,
    resolve_selectable_state,
)


def _listing(downloaded, catalog):
    return build_model_listing(downloaded, catalog)


# --- enabled mode -----------------------------------------------------------


@pytest.mark.tier1
def test_enabled_mode_offers_only_enabled_models():
    """At least one enabled model -> enabled mode, ``data`` = that model."""
    listing = _listing(
        {"data": [{"id": "whisper-small", "status": {"value": "loaded"}}]},
        [{"id": "whisper-small"}, {"id": "whisper-large-v3"}],
    )
    out = resolve_selectable_state(listing, {"whisper-small": True})

    assert out["mode"] == SELECTABLE_MODE_ENABLED
    assert [e["id"] for e in out["data"]] == ["whisper-small"]
    assert out["message"] is None
    # The active model is still reported alongside for context.
    assert out["active_model"] == "whisper-small"


@pytest.mark.tier1
def test_enabled_mode_when_enabled_model_is_not_the_active_one():
    """Enabled mode holds even when the enabled model differs from the active."""
    listing = _listing(
        {
            "data": [
                {"id": "whisper-small", "status": {"value": "loaded"}},
                {"id": "whisper-medium", "status": {"value": "unloaded"}},
            ]
        },
        [{"id": "whisper-small"}, {"id": "whisper-medium"}],
    )
    out = resolve_selectable_state(listing, {"whisper-medium": True})

    assert out["mode"] == SELECTABLE_MODE_ENABLED
    assert [e["id"] for e in out["data"]] == ["whisper-medium"]
    assert out["active_model"] == "whisper-small"


# --- fallback mode (R5 AC1) -------------------------------------------------


@pytest.mark.tier1
def test_fallback_when_downloaded_but_none_enabled():
    """Downloaded + serving but none enabled -> fallback to the active model."""
    listing = _listing(
        {"data": [{"id": "whisper-small", "status": {"value": "loaded"}}]},
        [{"id": "whisper-small"}, {"id": "whisper-large-v3"}],
    )
    out = resolve_selectable_state(listing, {})

    assert out["mode"] == SELECTABLE_MODE_FALLBACK
    assert out["data"] == []
    assert out["active_model"] == "whisper-small"
    assert out["fallback_model"]["id"] == "whisper-small"
    assert out["fallback_model"]["downloaded"] is True
    assert out["message"] is None


@pytest.mark.tier1
def test_fallback_ignores_a_disabled_flag_in_the_map():
    """An explicit ``False`` for the only model is still fallback, not enabled."""
    listing = _listing(
        {"data": [{"id": "whisper-small", "status": {"value": "active"}}]},
        [{"id": "whisper-small"}],
    )
    out = resolve_selectable_state(listing, {"whisper-small": False})

    assert out["mode"] == SELECTABLE_MODE_FALLBACK
    assert out["active_model"] == "whisper-small"


# --- unavailable mode (R5 AC2) ----------------------------------------------


@pytest.mark.tier1
def test_unavailable_when_zero_downloaded():
    """Zero downloaded (permitted no-active state) -> unavailable + message."""
    listing = _listing({"data": []}, [])
    out = resolve_selectable_state(listing, {})

    assert out["mode"] == SELECTABLE_MODE_UNAVAILABLE
    assert out["data"] == []
    assert out["active_model"] is None
    assert out["fallback_model"] is None
    assert isinstance(out["message"], str) and out["message"].strip()


@pytest.mark.tier1
def test_unavailable_when_only_pullable_entries_exist():
    """Catalog has pullable models but none downloaded -> still unavailable:
    a not-yet-downloaded model is not something an end user can transcribe with."""
    listing = _listing({"data": []}, [{"id": "whisper-large-v3"}])
    out = resolve_selectable_state(listing, {})

    assert out["mode"] == SELECTABLE_MODE_UNAVAILABLE
    assert out["active_model"] is None


@pytest.mark.tier1
def test_enabling_a_pullable_model_alone_does_not_leave_unavailable_but_offers_it():
    """Enabling a pullable-only model puts the picker in enabled mode offering it
    (the admin's curation intent), even though nothing is downloaded yet."""
    listing = _listing({"data": []}, [{"id": "whisper-large-v3"}])
    out = resolve_selectable_state(listing, {"whisper-large-v3": True})

    assert out["mode"] == SELECTABLE_MODE_ENABLED
    assert [e["id"] for e in out["data"]] == ["whisper-large-v3"]


@pytest.mark.tier1
def test_unavailable_when_downloaded_but_backend_reports_none_active():
    """Defensive R7-anomaly guard: models downloaded but the backend reports none
    active (an invariant violation) -> unavailable, never a fallback pointing at
    a model that is not actually serving."""
    listing = _listing(
        {"data": [{"id": "whisper-small", "status": {"value": "unloaded"}}]},
        [{"id": "whisper-small"}],
    )
    out = resolve_selectable_state(listing, {})

    assert out["mode"] == SELECTABLE_MODE_UNAVAILABLE
    assert out["active_model"] is None
    assert out["fallback_model"] is None


# --- transition (R5 AC3) ----------------------------------------------------


@pytest.mark.tier1
def test_enabling_transitions_fallback_to_enabled():
    """Enabling one model moves the same listing from fallback to enabled mode."""
    listing = _listing(
        {"data": [{"id": "whisper-small", "status": {"value": "loaded"}}]},
        [{"id": "whisper-small"}],
    )

    before = resolve_selectable_state(listing, {})
    assert before["mode"] == SELECTABLE_MODE_FALLBACK

    after = resolve_selectable_state(listing, {"whisper-small": True})
    assert after["mode"] == SELECTABLE_MODE_ENABLED
    assert [e["id"] for e in after["data"]] == ["whisper-small"]


@pytest.mark.tier1
def test_resolver_does_not_mutate_inputs():
    """Purity: the resolver copies entries and leaves the listing/map untouched."""
    listing = _listing(
        {"data": [{"id": "whisper-small", "status": {"value": "loaded"}}]},
        [{"id": "whisper-small"}],
    )
    snapshot = build_model_listing(
        {"data": [{"id": "whisper-small", "status": {"value": "loaded"}}]},
        [{"id": "whisper-small"}],
    )
    enabled = {"whisper-small": True}

    resolve_selectable_state(listing, enabled)

    assert listing == snapshot
    assert enabled == {"whisper-small": True}
