"""Unit tests for the transcribe-router model-listing merge logic.

Covers cavekit-audio-transcribe-router R1 at the pure-function level:
  AC1 — reports downloaded-and-ready models.
  AC2 — reports known catalog entries available to pull but not yet present.
  AC3 — a downloaded-and-ready model is distinguishable from a merely-pullable one.
"""

import pytest

from selfai_ui.transcribe.listing import (
    AVAILABILITY_DOWNLOADED,
    AVAILABILITY_PULLABLE,
    build_model_listing,
    curate_listing,
    filter_enabled_models,
)


def _by_id(listing):
    return {e["id"]: e for e in listing["data"]}


# ---------------------------------------------------------------------------
# curate_listing — admin curation overlay (cavekit-audio-transcribe-picker R1)
# ---------------------------------------------------------------------------


def _sample_listing():
    return build_model_listing(
        downloaded_raw={"data": [{"id": "whisper-small"}]},
        catalog_raw=[
            {"id": "whisper-small"},
            {"id": "whisper-large-v3", "description": "Large multilingual"},
        ],
    )


@pytest.mark.tier1
def test_curate_defaults_all_disabled():
    """Absent from the enabled map -> not enabled (conservative default)."""
    curated = curate_listing(_sample_listing(), enabled_map={})
    for entry in curated["data"]:
        assert entry["enabled"] is False


@pytest.mark.tier1
def test_curate_overlays_enabled_flag():
    """The enabled flag reflects the admin-set map, by model id."""
    curated = curate_listing(_sample_listing(), enabled_map={"whisper-small": True})
    by_id = _by_id(curated)
    assert by_id["whisper-small"]["enabled"] is True
    assert by_id["whisper-large-v3"]["enabled"] is False


@pytest.mark.tier1
def test_curate_can_enable_a_pullable_model():
    """A merely-pullable model may be enabled independent of download state."""
    curated = curate_listing(_sample_listing(), enabled_map={"whisper-large-v3": True})
    entry = _by_id(curated)["whisper-large-v3"]
    assert entry["downloaded"] is False
    assert entry["availability"] == AVAILABILITY_PULLABLE
    assert entry["enabled"] is True


@pytest.mark.tier1
def test_curate_search_filters_by_id_case_insensitive():
    """Search is a case-insensitive substring filter over the visible fields."""
    curated = curate_listing(_sample_listing(), enabled_map={}, search="LARGE")
    ids = {e["id"] for e in curated["data"]}
    assert ids == {"whisper-large-v3"}


@pytest.mark.tier1
def test_curate_search_matches_description():
    curated = curate_listing(_sample_listing(), enabled_map={}, search="multilingual")
    ids = {e["id"] for e in curated["data"]}
    assert ids == {"whisper-large-v3"}


@pytest.mark.tier1
def test_curate_empty_search_returns_all():
    curated = curate_listing(_sample_listing(), enabled_map={}, search="   ")
    assert len(curated["data"]) == 2


@pytest.mark.tier1
def test_curate_does_not_mutate_input():
    """curate_listing is pure — the source listing entries are unchanged."""
    listing = _sample_listing()
    curate_listing(listing, enabled_map={"whisper-small": True})
    # The source listing's entries keep their base (disabled) state.
    assert all(e["enabled"] is False for e in listing["data"])


# ---------------------------------------------------------------------------
# R2 (transcribe-picker) — the downloaded-vs-pullable distinction survives the
# admin-curation overlay, so it is legible in the curation list itself (T-029).
#
# The distinction is produced by build_model_listing (R1) as downloaded +
# availability on every entry. T-029's concern is that the curation overlay
# (curate_listing, which adds enabled and applies search) neither drops nor
# corrupts that distinction — i.e. it stays present, per-entry, and internally
# consistent on the SAME payload that carries the curation data.
# ---------------------------------------------------------------------------


@pytest.mark.tier1
def test_curate_preserves_distinction_on_every_entry():
    """R2 AC1: every curation-list entry still carries both downloaded and
    availability after the enabled overlay is applied."""
    curated = curate_listing(_sample_listing(), enabled_map={"whisper-small": True})
    assert curated["data"], "expected a non-empty curated listing"
    for entry in curated["data"]:
        assert "downloaded" in entry
        assert "availability" in entry
        # The overlay adds enabled without displacing the distinction fields.
        assert "enabled" in entry


@pytest.mark.tier1
def test_curate_keeps_downloaded_and_availability_consistent():
    """The two redundant discriminators never disagree after curation: an entry
    is (downloaded=True, availability=downloaded) or (False, pullable)."""
    curated = curate_listing(_sample_listing(), enabled_map={"whisper-large-v3": True})
    for entry in curated["data"]:
        if entry["downloaded"] is True:
            assert entry["availability"] == AVAILABILITY_DOWNLOADED
        else:
            assert entry["downloaded"] is False
            assert entry["availability"] == AVAILABILITY_PULLABLE


@pytest.mark.tier1
def test_curate_distinction_and_enabled_coexist_in_one_payload():
    """R2 AC2: the download distinction rides on the same curated response as the
    curation (enabled) data — no separate management round-trip is needed to tell
    downloaded from pullable."""
    curated = curate_listing(_sample_listing(), enabled_map={})
    for entry in curated["data"]:
        assert {"downloaded", "availability", "enabled"} <= set(entry)


@pytest.mark.tier1
def test_curate_enabling_pullable_does_not_flip_distinction():
    """Enabling a not-yet-downloaded model must not make it look downloaded — the
    curation flag and the download distinction are orthogonal."""
    curated = curate_listing(_sample_listing(), enabled_map={"whisper-large-v3": True})
    entry = _by_id(curated)["whisper-large-v3"]
    assert entry["enabled"] is True
    assert entry["downloaded"] is False
    assert entry["availability"] == AVAILABILITY_PULLABLE


@pytest.mark.tier1
def test_curate_search_survivors_keep_the_distinction():
    """A search-filtered curation list still distinguishes its survivors."""
    curated = curate_listing(_sample_listing(), enabled_map={}, search="whisper")
    assert len(curated["data"]) == 2
    for entry in curated["data"]:
        assert "downloaded" in entry
        assert entry["availability"] in (AVAILABILITY_DOWNLOADED, AVAILABILITY_PULLABLE)


@pytest.mark.tier0
def test_reports_downloaded_models():
    """AC1: models the backend actually holds are reported as downloaded."""
    downloaded = {"data": [{"id": "whisper-small", "status": {"value": "loaded"}}]}
    listing = build_model_listing(downloaded, catalog_raw=None)

    entry = _by_id(listing)["whisper-small"]
    assert entry["downloaded"] is True
    assert entry["availability"] == AVAILABILITY_DOWNLOADED
    assert entry["status"] == "loaded"


@pytest.mark.tier0
def test_reports_pullable_catalog_entries():
    """AC2: known catalog entries not present are reported as pullable."""
    catalog = [
        {"id": "whisper-large-v3", "description": "Large multilingual"},
        {"id": "whisper-medium"},
    ]
    listing = build_model_listing(downloaded_raw=None, catalog_raw=catalog)
    by_id = _by_id(listing)

    assert set(by_id) == {"whisper-large-v3", "whisper-medium"}
    for entry in by_id.values():
        assert entry["downloaded"] is False
        assert entry["availability"] == AVAILABILITY_PULLABLE
    assert by_id["whisper-large-v3"]["description"] == "Large multilingual"


@pytest.mark.tier0
def test_downloaded_distinguishable_from_pullable():
    """AC3: the two are simultaneously present and clearly distinguished."""
    downloaded = {"data": [{"id": "whisper-small", "status": {"value": "unloaded"}}]}
    catalog = [
        {"id": "whisper-small"},  # already downloaded — must NOT be pullable
        {"id": "whisper-large-v3", "description": "Large"},  # only pullable
    ]
    listing = build_model_listing(downloaded, catalog)
    by_id = _by_id(listing)

    # Both surfaces represented.
    assert by_id["whisper-small"]["availability"] == AVAILABILITY_DOWNLOADED
    assert by_id["whisper-small"]["downloaded"] is True
    assert by_id["whisper-large-v3"]["availability"] == AVAILABILITY_PULLABLE
    assert by_id["whisper-large-v3"]["downloaded"] is False

    # A model present on disk is never double-reported as pullable.
    smalls = [e for e in listing["data"] if e["id"] == "whisper-small"]
    assert len(smalls) == 1

    # The discriminator partitions the listing with no ambiguity.
    downloaded_ids = {e["id"] for e in listing["data"] if e["availability"] == AVAILABILITY_DOWNLOADED}
    pullable_ids = {e["id"] for e in listing["data"] if e["availability"] == AVAILABILITY_PULLABLE}
    assert downloaded_ids.isdisjoint(pullable_ids)
    assert downloaded_ids == {"whisper-small"}
    assert pullable_ids == {"whisper-large-v3"}


@pytest.mark.tier0
def test_downloaded_model_inherits_catalog_description():
    """A downloaded model still gets catalog metadata (e.g. description)."""
    downloaded = [{"id": "whisper-small"}]
    catalog = [{"id": "whisper-small", "description": "Small English"}]
    listing = build_model_listing(downloaded, catalog)
    entry = _by_id(listing)["whisper-small"]
    assert entry["downloaded"] is True
    assert entry["description"] == "Small English"


@pytest.mark.tier0
def test_downloaded_not_in_catalog_still_reported():
    """A downloaded model absent from the catalog is still downloaded-ready."""
    downloaded = [{"id": "custom-finetune", "status": "loaded"}]
    listing = build_model_listing(downloaded, catalog_raw=[])
    entry = _by_id(listing)["custom-finetune"]
    assert entry["downloaded"] is True
    assert entry["availability"] == AVAILABILITY_DOWNLOADED
    assert entry["status"] == "loaded"


@pytest.mark.tier0
def test_empty_backend_yields_empty_listing():
    """Fresh backend: nothing downloaded, nothing catalogued -> empty listing."""
    assert build_model_listing(None, None) == {"data": []}
    assert build_model_listing({"data": []}, []) == {"data": []}


@pytest.mark.tier0
def test_tolerates_malformed_views():
    """Malformed/unexpected view shapes degrade to empty, never raise."""
    assert build_model_listing("garbage", 12345) == {"data": []}
    # Rows without a usable id are skipped rather than crashing.
    listing = build_model_listing({"data": [{"foo": "bar"}, {"id": "ok"}]}, None)
    assert _by_id(listing).keys() == {"ok"}


@pytest.mark.tier0
def test_models_key_wrapper_accepted():
    """Backend may wrap rows under 'models' instead of 'data'."""
    downloaded = {"models": [{"id": "whisper-base"}]}
    listing = build_model_listing(downloaded, None)
    assert _by_id(listing)["whisper-base"]["downloaded"] is True


# ---------------------------------------------------------------------------
# filter_enabled_models — end-user selectable set (cavekit-audio-transcribe-picker R4)
# ---------------------------------------------------------------------------


@pytest.mark.tier1
def test_filter_returns_only_enabled():
    """R4 AC2: only models an admin has enabled (id -> True) are selectable."""
    selectable = filter_enabled_models(
        _sample_listing(), enabled_map={"whisper-small": True}
    )
    by_id = _by_id(selectable)
    assert set(by_id) == {"whisper-small"}
    assert by_id["whisper-small"]["enabled"] is True


@pytest.mark.tier1
def test_filter_excludes_disabled_and_absent():
    """R4 AC3: a disabled (False) or unmapped model is not selectable."""
    selectable = filter_enabled_models(
        _sample_listing(),
        enabled_map={"whisper-small": False},  # whisper-large-v3 absent entirely
    )
    assert selectable == {"data": []}


@pytest.mark.tier1
def test_filter_only_strict_true_qualifies():
    """A non-True truthy value does not enable — the contract is strict `is True`."""
    selectable = filter_enabled_models(
        _sample_listing(), enabled_map={"whisper-small": 1, "whisper-large-v3": "yes"}
    )
    assert selectable == {"data": []}


@pytest.mark.tier1
def test_filter_includes_enabled_pullable():
    """An admin-enabled but merely-pullable model is still selectable."""
    selectable = filter_enabled_models(
        _sample_listing(), enabled_map={"whisper-large-v3": True}
    )
    by_id = _by_id(selectable)
    assert set(by_id) == {"whisper-large-v3"}
    assert by_id["whisper-large-v3"]["availability"] == AVAILABILITY_PULLABLE


@pytest.mark.tier1
def test_filter_empty_map_yields_nothing():
    """No admin has enabled anything -> nothing selectable (empty, not error)."""
    assert filter_enabled_models(_sample_listing(), enabled_map={}) == {"data": []}
    assert filter_enabled_models(_sample_listing(), enabled_map=None) == {"data": []}
