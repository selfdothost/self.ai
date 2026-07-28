"""Unit tests for the self-hosted TTS voice-catalog build logic.

Covers cavekit-audio-voice-catalog R1 at the pure-function level:
  AC1 — the voice list matches the voices the connection actually offers.
  AC2 — two connections with different voice sets return different lists
        (no shared hardcoded placeholder).
  AC3 — a voice absent from a connection's actual offering does not appear.

And cavekit-audio-voice-catalog R2 (voice metadata) at the same level:
  AC1 — each voice carries a human-readable name.
  AC2 — each voice carries a language attribute.
  AC3 — each voice carries a gender attribute.
  AC4 — a voice is never represented by identifier alone with these attributes
        absent (name always resolves, falling back to the id).
"""

import pytest

from selfai_ui.audio.connections import (
    AudioConnectionType,
    SavedAudioConnection,
    connection_display_label,
)
from selfai_ui.audio.voice_catalog import (
    aggregate_voice_catalogs,
    build_voice_catalog,
    curate_voice_catalog,
    disambiguate_source_labels,
    filter_enabled_voices,
    label_voice_sources,
    resolve_tts_control_url,
)


def _ids(catalog):
    return [v["id"] for v in catalog["voices"]]


def _by_id(catalog, voice_id):
    return next(v for v in catalog["voices"] if v["id"] == voice_id)


@pytest.mark.tier0
def test_catalog_matches_actual_backend_voices():
    """AC1: the catalog is exactly what the connection's backend reported."""
    backend = {"voices": [{"id": "en-us-amy"}, {"id": "en-us-ryan"}]}
    catalog = build_voice_catalog(backend)
    assert _ids(catalog) == ["en-us-amy", "en-us-ryan"]


@pytest.mark.tier0
def test_two_connections_return_different_lists():
    """AC2: different backends -> different catalogs, no shared placeholder."""
    backend_a = {"voices": [{"id": "alpha-1"}, {"id": "alpha-2"}]}
    backend_b = {"voices": [{"id": "beta-1"}]}

    catalog_a = build_voice_catalog(backend_a)
    catalog_b = build_voice_catalog(backend_b)

    assert _ids(catalog_a) == ["alpha-1", "alpha-2"]
    assert _ids(catalog_b) == ["beta-1"]
    # No overlap and no injected default set common to both.
    assert set(_ids(catalog_a)).isdisjoint(_ids(catalog_b))


@pytest.mark.tier0
def test_absent_voice_not_in_catalog():
    """AC3: a voice the backend never reported is never synthesized in."""
    backend = {"voices": [{"id": "present-voice"}]}
    catalog = build_voice_catalog(backend)
    assert "absent-voice" not in _ids(catalog)
    assert _ids(catalog) == ["present-voice"]


@pytest.mark.tier0
def test_empty_backend_yields_empty_catalog():
    """A backend offering no voices yields an empty catalog, not a default set."""
    assert build_voice_catalog({"voices": []}) == {"voices": []}
    assert build_voice_catalog([]) == {"voices": []}
    assert build_voice_catalog(None) == {"voices": []}


@pytest.mark.tier0
def test_bare_list_shape_accepted():
    """Backend may return a bare list rather than a {'voices': [...]} wrapper."""
    catalog = build_voice_catalog([{"id": "v1"}, {"id": "v2"}])
    assert _ids(catalog) == ["v1", "v2"]


@pytest.mark.tier0
def test_data_key_wrapper_accepted():
    """Backend may wrap rows under 'data' instead of 'voices'."""
    catalog = build_voice_catalog({"data": [{"id": "v9"}]})
    assert _ids(catalog) == ["v9"]


@pytest.mark.tier0
def test_bare_string_rows_accepted():
    """Backend may report voices as bare id strings, not dicts."""
    catalog = build_voice_catalog(["nova", "onyx"])
    assert _ids(catalog) == ["nova", "onyx"]


@pytest.mark.tier0
def test_alternate_id_keys_accepted():
    """A voice id may arrive under voice_id/name/voice, not just id."""
    backend = {"voices": [{"voice_id": "vid-1"}, {"name": "vname"}, {"voice": "vv"}]}
    catalog = build_voice_catalog(backend)
    assert _ids(catalog) == ["vid-1", "vname", "vv"]


@pytest.mark.tier0
def test_duplicate_voices_deduplicated_order_preserved():
    """Repeated ids collapse to one, first-seen order preserved."""
    backend = {"voices": [{"id": "a"}, {"id": "b"}, {"id": "a"}]}
    catalog = build_voice_catalog(backend)
    assert _ids(catalog) == ["a", "b"]


@pytest.mark.tier0
def test_malformed_rows_skipped_not_fabricated():
    """Rows without a usable id are dropped rather than crashing or inventing."""
    backend = {"voices": [{"foo": "bar"}, {"id": ""}, 12345, {"id": "ok"}]}
    catalog = build_voice_catalog(backend)
    assert _ids(catalog) == ["ok"]


@pytest.mark.tier0
def test_garbage_input_degrades_to_empty():
    """Wholly unexpected input degrades to an empty catalog, never raises."""
    assert build_voice_catalog("garbage") == {"voices": []}
    assert build_voice_catalog(42) == {"voices": []}


# --- R2: Voice Metadata ------------------------------------------------------


@pytest.mark.tier0
def test_voice_carries_human_readable_name():
    """R2 AC1: each voice carries a human-readable name from the backend."""
    backend = {"voices": [{"id": "en-us-amy", "name": "Amy"}]}
    entry = _by_id(build_voice_catalog(backend), "en-us-amy")
    assert entry["name"] == "Amy"


@pytest.mark.tier0
def test_voice_carries_language():
    """R2 AC2: each voice carries a language attribute."""
    backend = {"voices": [{"id": "en-us-amy", "language": "en-US"}]}
    entry = _by_id(build_voice_catalog(backend), "en-us-amy")
    assert entry["language"] == "en-US"


@pytest.mark.tier0
def test_voice_carries_gender():
    """R2 AC3: each voice carries a gender attribute."""
    backend = {"voices": [{"id": "en-us-amy", "gender": "female"}]}
    entry = _by_id(build_voice_catalog(backend), "en-us-amy")
    assert entry["gender"] == "female"


@pytest.mark.tier0
def test_full_metadata_row_populates_every_field():
    """R2 AC1-AC3 together: a fully-described voice carries all three."""
    backend = {
        "voices": [
            {"id": "en-us-ryan", "name": "Ryan", "language": "en-US", "gender": "male"}
        ]
    }
    entry = _by_id(build_voice_catalog(backend), "en-us-ryan")
    assert entry == {
        "id": "en-us-ryan",
        "name": "Ryan",
        "language": "en-US",
        "gender": "male",
    }


@pytest.mark.tier0
def test_name_always_present_falling_back_to_id():
    """R2 AC4: a voice with no backend-supplied name still carries a name (its id).

    The entry is never a bare identifier with the name field absent — name is a
    required field that defaults to the id.
    """
    backend = {"voices": [{"id": "bare-voice"}]}
    entry = _by_id(build_voice_catalog(backend), "bare-voice")
    assert entry["name"] == "bare-voice"
    # Every entry always carries all three metadata keys, present even when the
    # backend reported nothing for language/gender.
    assert set(entry.keys()) == {"id", "name", "language", "gender"}


@pytest.mark.tier0
def test_bare_string_row_gets_name_from_id():
    """R2 AC4: a bare-string voice row resolves name to the id, metadata None."""
    catalog = build_voice_catalog(["nova"])
    entry = _by_id(catalog, "nova")
    assert entry["name"] == "nova"
    assert entry["language"] is None
    assert entry["gender"] is None


@pytest.mark.tier0
def test_missing_language_and_gender_are_none_not_fabricated():
    """Language/gender absent from the backend row -> None, never invented."""
    backend = {"voices": [{"id": "v1", "name": "Vee"}]}
    entry = _by_id(build_voice_catalog(backend), "v1")
    assert entry["language"] is None
    assert entry["gender"] is None


@pytest.mark.tier0
def test_name_alternate_keys_tolerated():
    """Name may arrive under display_name or label, not just name."""
    backend = {
        "voices": [
            {"id": "v1", "display_name": "Display One"},
            {"id": "v2", "label": "Label Two"},
        ]
    }
    catalog = build_voice_catalog(backend)
    assert _by_id(catalog, "v1")["name"] == "Display One"
    assert _by_id(catalog, "v2")["name"] == "Label Two"


@pytest.mark.tier0
def test_language_alternate_keys_tolerated():
    """Language may arrive under lang or locale, not just language."""
    backend = {
        "voices": [
            {"id": "v1", "lang": "de"},
            {"id": "v2", "locale": "fr-FR"},
        ]
    }
    catalog = build_voice_catalog(backend)
    assert _by_id(catalog, "v1")["language"] == "de"
    assert _by_id(catalog, "v2")["language"] == "fr-FR"


@pytest.mark.tier0
def test_gender_alternate_key_tolerated():
    """Gender may arrive under sex, not just gender."""
    backend = {"voices": [{"id": "v1", "sex": "male"}]}
    entry = _by_id(build_voice_catalog(backend), "v1")
    assert entry["gender"] == "male"


@pytest.mark.tier0
def test_every_voice_carries_all_three_metadata_fields():
    """R2 AC4 (catalog-wide): no voice is a bare id — all carry name/lang/gender.

    Even a mix of fully-, partially-, and un-described voices each expose the
    three metadata keys, with name always non-empty.
    """
    backend = {
        "voices": [
            {"id": "full", "name": "Full", "language": "en", "gender": "female"},
            {"id": "partial", "name": "Partial"},
            {"id": "bare"},
            "string-voice",
        ]
    }
    catalog = build_voice_catalog(backend)
    for entry in catalog["voices"]:
        assert set(entry.keys()) == {"id", "name", "language", "gender"}
        assert isinstance(entry["name"], str) and entry["name"]


# ---------------------------------------------------------------------------
# filter_enabled_voices — enabled-only end-user catalog (R5, T-014)
#   AC1 — the end-user catalog omits any voice an admin has disabled.
#   AC2 — a disabled voice remains present in the (unfiltered) admin catalog.
#   AC3 — enabling a disabled voice makes it appear; disabling removes it.
# ---------------------------------------------------------------------------


def _catalog(*ids):
    return {"voices": [{"id": vid} for vid in ids]}


@pytest.mark.tier0
def test_filter_omits_disabled_voices():
    """AC1: only voices mapped True survive into the end-user catalog."""
    catalog = _catalog("a", "b", "c")
    enabled = filter_enabled_voices(catalog, {"a": True, "b": False, "c": True})
    assert _ids(enabled) == ["a", "c"]


@pytest.mark.tier0
def test_filter_absent_id_treated_as_disabled():
    """A voice absent from the map is disabled by default (conservative)."""
    catalog = _catalog("a", "b")
    enabled = filter_enabled_voices(catalog, {"a": True})
    assert _ids(enabled) == ["a"]


@pytest.mark.tier0
def test_filter_empty_map_yields_no_voices():
    """None enabled -> the end-user catalog is empty (admins curate voices in)."""
    catalog = _catalog("a", "b", "c")
    assert filter_enabled_voices(catalog, {}) == {"voices": []}


@pytest.mark.tier0
def test_filter_does_not_mutate_admin_catalog():
    """AC2: filtering leaves the full admin catalog untouched — a disabled
    voice is still present in the source the admin view returns."""
    catalog = _catalog("a", "b")
    filter_enabled_voices(catalog, {"a": True})
    # The source (admin) catalog still carries the disabled voice "b".
    assert _ids(catalog) == ["a", "b"]


@pytest.mark.tier0
def test_filter_reflects_toggle_both_directions():
    """AC3: enabling adds the voice; disabling removes it."""
    catalog = _catalog("a", "b")
    # Disabled -> absent.
    assert _ids(filter_enabled_voices(catalog, {"a": True, "b": False})) == ["a"]
    # Enable b -> now present.
    assert _ids(filter_enabled_voices(catalog, {"a": True, "b": True})) == ["a", "b"]
    # Disable a -> now absent.
    assert _ids(filter_enabled_voices(catalog, {"a": False, "b": True})) == ["b"]


@pytest.mark.tier0
def test_filter_only_strict_true_enables():
    """Truthy-but-not-True values do not enable — the map is a strict bool map."""
    catalog = _catalog("a", "b", "c")
    enabled = filter_enabled_voices(catalog, {"a": 1, "b": "yes", "c": True})
    assert _ids(enabled) == ["c"]


@pytest.mark.tier0
def test_filter_tolerates_non_dict_map():
    """A malformed (non-dict) enabled map degrades to nothing enabled."""
    catalog = _catalog("a", "b")
    assert filter_enabled_voices(catalog, None) == {"voices": []}
    assert filter_enabled_voices(catalog, "garbage") == {"voices": []}


@pytest.mark.tier0
def test_filter_empty_catalog_stays_empty():
    """Filtering an empty catalog yields an empty end-user catalog."""
    assert filter_enabled_voices({"voices": []}, {"a": True}) == {"voices": []}
    assert filter_enabled_voices({}, {"a": True}) == {"voices": []}


@pytest.mark.tier0
def test_filter_preserves_extra_entry_fields():
    """Filtering carries through any additional voice metadata (T-011 fields)."""
    catalog = {"voices": [{"id": "a", "name": "Amy", "language": "en"}]}
    enabled = filter_enabled_voices(catalog, {"a": True})
    assert enabled["voices"] == [{"id": "a", "name": "Amy", "language": "en"}]


# ---------------------------------------------------------------------------
# R3 (cavekit-audio-voice-picker): Selection Uses Real Metadata (T-017)
#   AC1 — each selectable voice is presented with its name, language and gender.
#   AC2 — selection is made from a real selector (structured metadata-bearing
#         entries), not a free-text entry field.
#   AC3 — no free-text fallback path for entering a voice remains in the
#         end-user picker: a voice can only be offered if it is a real catalog
#         entry (metadata-bearing), never conjured from a bare id/free-text.
#
# The end-user picker's data source is ``GET /voices/selectable`` =
# ``_aggregate_catalog`` (:func:`aggregate_voice_catalogs` -> :func:`build_voice_catalog`,
# every entry carrying R2 name/language/gender) narrowed by
# :func:`filter_enabled_voices`. These pure tests prove the selector-facing data
# a real selector reads is always full-metadata and is never a bare identifier;
# the router-level proof lives in ``routers/test_voice_catalog.py``.
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_selectable_data_carries_full_metadata_for_the_selector():
    """R3 AC1: the enabled-only picker data carries name/language/gender per voice.

    A real selector reads exactly this: every offered voice arrives as a
    structured entry with all three human-facing fields, so the picker never
    needs a free-text field to name a voice.
    """
    catalog = build_voice_catalog(
        {
            "voices": [
                {"id": "en-us-amy", "name": "Amy", "language": "en-US", "gender": "female"},
                {"id": "de-de-max", "name": "Max", "language": "de-DE", "gender": "male"},
            ]
        }
    )
    selectable = filter_enabled_voices(catalog, {"en-us-amy": True, "de-de-max": True})
    for entry in selectable["voices"]:
        # AC1: all three metadata fields are present on every offered voice.
        assert {"id", "name", "language", "gender"} <= set(entry.keys())
        assert isinstance(entry["name"], str) and entry["name"]
    by_id = {v["id"]: v for v in selectable["voices"]}
    assert by_id["en-us-amy"]["language"] == "en-US"
    assert by_id["en-us-amy"]["gender"] == "female"
    assert by_id["de-de-max"]["language"] == "de-DE"
    assert by_id["de-de-max"]["gender"] == "male"


@pytest.mark.tier0
def test_selectable_entry_is_structured_never_bare_identifier():
    """R3 AC1/AC2: even a backend-nameless voice is offered as a structured entry
    with a name (its id) and explicit null language/gender — never a bare string
    identifier the UI would have to render in a free-text box."""
    catalog = build_voice_catalog({"voices": [{"id": "bare-voice"}]})
    selectable = filter_enabled_voices(catalog, {"bare-voice": True})
    entry = selectable["voices"][0]
    assert isinstance(entry, dict)  # a structured selector row, not a bare id str
    assert entry["name"] == "bare-voice"  # name always present (R2 AC4)
    assert entry["language"] is None
    assert entry["gender"] is None
    assert set(entry.keys()) == {"id", "name", "language", "gender"}


@pytest.mark.tier0
def test_selectable_cannot_offer_a_voice_absent_from_the_real_catalog():
    """R3 AC3: a voice enabled by id but absent from the real backend catalog is
    NOT offered — the picker can only present real, metadata-bearing catalog
    entries, so there is no free-text/bare-id path to inject an arbitrary voice."""
    catalog = build_voice_catalog({"voices": [{"id": "real-voice", "name": "Real"}]})
    # 'ghost' is enabled in the admin map but the backend never reported it.
    selectable = filter_enabled_voices(catalog, {"real-voice": True, "ghost": True})
    ids = [v["id"] for v in selectable["voices"]]
    assert ids == ["real-voice"]  # 'ghost' cannot be conjured into the picker
    assert "ghost" not in ids


# ---------------------------------------------------------------------------
# aggregate_voice_catalogs — multi-connection aggregation w/ source attribution
# (cavekit-audio-voice-catalog R3, T-012)
#   AC1 — with two TTS connections configured, the aggregated catalog contains
#         voices from both.
#   AC2 — each voice carries the identity of the connection it originated from.
#   AC3 — two similarly-named voices from different connections remain
#         individually distinguishable by their source connection.
# ---------------------------------------------------------------------------


def _sources_by_id(catalog):
    return [(v["id"], v["source_connection_id"]) for v in catalog["voices"]]


@pytest.mark.tier0
def test_aggregate_contains_voices_from_both_connections():
    """AC1: the merged catalog carries every source connection's voices."""
    sources = [
        ("conn-a", {"voices": [{"id": "amy"}, {"id": "ryan"}]}),
        ("conn-b", {"voices": [{"id": "nova"}]}),
    ]
    catalog = aggregate_voice_catalogs(sources)
    assert _ids(catalog) == ["amy", "ryan", "nova"]


@pytest.mark.tier0
def test_aggregate_each_voice_carries_source_connection():
    """AC2: every aggregated voice is tagged with its source connection id."""
    sources = [
        ("conn-a", {"voices": [{"id": "amy"}]}),
        ("conn-b", {"voices": [{"id": "nova"}]}),
    ]
    catalog = aggregate_voice_catalogs(sources)
    assert _sources_by_id(catalog) == [("amy", "conn-a"), ("nova", "conn-b")]
    for entry in catalog["voices"]:
        assert entry["source_connection_id"]


@pytest.mark.tier0
def test_aggregate_same_id_from_two_connections_both_kept():
    """AC3: a voice id offered by two connections yields two distinct, attributed
    entries — never collapsed into one — so each stays reachable by its source."""
    sources = [
        ("conn-a", {"voices": [{"id": "en-us-amy", "name": "Amy"}]}),
        ("conn-b", {"voices": [{"id": "en-us-amy", "name": "Amy"}]}),
    ]
    catalog = aggregate_voice_catalogs(sources)
    assert _sources_by_id(catalog) == [
        ("en-us-amy", "conn-a"),
        ("en-us-amy", "conn-b"),
    ]


@pytest.mark.tier0
def test_aggregate_similarly_named_distinguishable_by_source():
    """AC3: two voices with the same human name but different backends stay
    distinguishable via source_connection_id."""
    sources = [
        ("studio", {"voices": [{"id": "amy-1", "name": "Amy"}]}),
        ("home", {"voices": [{"id": "amy-2", "name": "Amy"}]}),
    ]
    catalog = aggregate_voice_catalogs(sources)
    names = [(v["name"], v["source_connection_id"]) for v in catalog["voices"]]
    assert names == [("Amy", "studio"), ("Amy", "home")]
    # Both are named "Amy" yet remain individually addressable by source.
    assert len({v["source_connection_id"] for v in catalog["voices"]}) == 2


@pytest.mark.tier0
def test_aggregate_preserves_voice_metadata():
    """Aggregation carries through each voice's T-011 name/language/gender."""
    sources = [
        (
            "conn-a",
            {"voices": [{"id": "amy", "name": "Amy", "language": "en-US", "gender": "female"}]},
        ),
    ]
    catalog = aggregate_voice_catalogs(sources)
    assert catalog["voices"][0] == {
        "id": "amy",
        "name": "Amy",
        "language": "en-US",
        "gender": "female",
        "source_connection_id": "conn-a",
    }


@pytest.mark.tier0
def test_aggregate_empty_or_unreachable_source_contributes_nothing():
    """A source with an empty or None (unreachable) response adds no voices and
    does not break the aggregate; other sources still merge."""
    sources = [
        ("conn-a", {"voices": [{"id": "amy"}]}),
        ("conn-empty", {"voices": []}),
        ("conn-unreachable", None),
        ("conn-b", {"voices": [{"id": "nova"}]}),
    ]
    catalog = aggregate_voice_catalogs(sources)
    assert _sources_by_id(catalog) == [("amy", "conn-a"), ("nova", "conn-b")]


@pytest.mark.tier0
def test_aggregate_no_sources_yields_empty_catalog():
    """No configured connections -> an empty aggregate, never a placeholder set."""
    assert aggregate_voice_catalogs([]) == {"voices": []}
    assert aggregate_voice_catalogs(None) == {"voices": []}


@pytest.mark.tier0
def test_aggregate_dedups_within_a_single_source_by_id():
    """A source repeating an id collapses it once (single-connection dedup carries
    through), while the same id from a *different* source is still kept."""
    sources = [
        ("conn-a", {"voices": [{"id": "amy"}, {"id": "amy"}, {"id": "ryan"}]}),
        ("conn-b", {"voices": [{"id": "amy"}]}),
    ]
    catalog = aggregate_voice_catalogs(sources)
    assert _sources_by_id(catalog) == [
        ("amy", "conn-a"),
        ("ryan", "conn-a"),
        ("amy", "conn-b"),
    ]


@pytest.mark.tier0
def test_aggregate_order_is_source_then_within_source():
    """Merge order is source order, then each connection's own voice order."""
    sources = [
        ("first", {"voices": [{"id": "a1"}, {"id": "a2"}]}),
        ("second", {"voices": [{"id": "b1"}, {"id": "b2"}]}),
    ]
    catalog = aggregate_voice_catalogs(sources)
    assert _ids(catalog) == ["a1", "a2", "b1", "b2"]


# ---------------------------------------------------------------------------
# resolve_tts_control_url — the fallback address-resolution rule (T-012)
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_resolve_falls_back_to_shared_control_url():
    """The current SELF_HOSTED_TTS field set (model/split_on) carries no address,
    so resolution falls back to the shared AUDIO_TTS_CONTROL_BASE_URL."""
    fields = {"model": "speaker.onnx", "split_on": "."}
    assert (
        resolve_tts_control_url(fields, "http://self-speak:9100")
        == "http://self-speak:9100"
    )


@pytest.mark.tier0
def test_resolve_prefers_per_connection_address_when_present():
    """A per-connection address field, if a future field set adds one, wins over
    the shared fallback."""
    fields = {"control_base_url": "http://per-conn:9100", "model": "m"}
    assert (
        resolve_tts_control_url(fields, "http://shared:9100")
        == "http://per-conn:9100"
    )


@pytest.mark.tier0
def test_resolve_none_when_no_address_anywhere():
    """No per-connection address and no shared fallback -> None (skip it)."""
    assert resolve_tts_control_url({"model": "m"}, "") is None
    assert resolve_tts_control_url({"model": "m"}, None) is None


@pytest.mark.tier0
def test_resolve_strips_whitespace_and_ignores_empty_fields():
    """Whitespace-only field values are ignored; resolved values are stripped."""
    assert resolve_tts_control_url({"control_base_url": "   "}, "http://s:1") == "http://s:1"
    assert resolve_tts_control_url({}, "  http://s:1  ") == "http://s:1"


@pytest.mark.tier0
def test_resolve_tolerates_non_dict_fields():
    """A malformed (non-dict) fields value degrades to the shared fallback."""
    assert resolve_tts_control_url(None, "http://s:1") == "http://s:1"
    assert resolve_tts_control_url("garbage", "http://s:1") == "http://s:1"


# ---------------------------------------------------------------------------
# curate_voice_catalog — the admin curation surface overlay
# (cavekit-audio-voice-picker R1, T-015)
#   AC1 — the admin surface lists EVERY voice, including currently-disabled ones.
#   AC2 — each listed voice carries a toggleable enabled state.
#   AC3 — the list is searchable (text-model curation pattern).
#   AC4 — a toggle persists and is reflected on reload (enabled overlay re-reads
#         the same map; the persistence itself is exercised at the router level).
# ---------------------------------------------------------------------------


def _agg_catalog(*specs):
    """Build an aggregated-shape catalog from (id, source[, name]) specs."""
    voices = []
    for spec in specs:
        vid, source = spec[0], spec[1]
        entry = {"id": vid, "source_connection_id": source}
        if len(spec) > 2:
            entry["name"] = spec[2]
        voices.append(entry)
    return {"voices": voices}


@pytest.mark.tier0
def test_curate_lists_every_voice_including_disabled():
    """AC1: every voice is carried through, disabled ones included."""
    catalog = _agg_catalog(("a", "c1"), ("b", "c1"), ("c", "c2"))
    curated = curate_voice_catalog(catalog, {"a": True, "b": False})
    # All three remain listed — the overlay annotates, it does not filter.
    assert _ids(curated) == ["a", "b", "c"]


@pytest.mark.tier0
def test_curate_each_voice_carries_enabled_flag():
    """AC2: every listed voice gains a bool enabled flag from the map."""
    catalog = _agg_catalog(("a", "c1"), ("b", "c1"), ("c", "c2"))
    curated = curate_voice_catalog(catalog, {"a": True, "b": False})
    flags = {v["id"]: v["enabled"] for v in curated["voices"]}
    assert flags == {"a": True, "b": False, "c": False}
    # A voice absent from the map defaults to disabled (conservative "curate in").
    for v in curated["voices"]:
        assert isinstance(v["enabled"], bool)


@pytest.mark.tier0
def test_curate_only_strict_true_is_enabled():
    """AC2: truthy-but-not-True map values are treated as disabled."""
    catalog = _agg_catalog(("a", "c1"), ("b", "c1"), ("c", "c2"))
    curated = curate_voice_catalog(catalog, {"a": 1, "b": "yes", "c": True})
    flags = {v["id"]: v["enabled"] for v in curated["voices"]}
    assert flags == {"a": False, "b": False, "c": True}


@pytest.mark.tier0
def test_curate_search_filters_by_name():
    """AC3: a search needle narrows the list (case-insensitive) by name."""
    catalog = {
        "voices": [
            {"id": "v1", "name": "Amy", "source_connection_id": "c1"},
            {"id": "v2", "name": "Ryan", "source_connection_id": "c1"},
        ]
    }
    curated = curate_voice_catalog(catalog, {}, search="amy")
    assert _ids(curated) == ["v1"]


@pytest.mark.tier0
def test_curate_search_matches_id_language_gender_and_source():
    """AC3: search spans id, language, gender and source connection too."""
    catalog = {
        "voices": [
            {"id": "en-us-amy", "name": "Amy", "language": "en-US",
             "gender": "female", "source_connection_id": "studio"},
            {"id": "de-de-max", "name": "Max", "language": "de-DE",
             "gender": "male", "source_connection_id": "home"},
        ]
    }
    # by id fragment
    assert _ids(curate_voice_catalog(catalog, {}, search="de-de")) == ["de-de-max"]
    # by language
    assert _ids(curate_voice_catalog(catalog, {}, search="en-us")) == ["en-us-amy"]
    # by gender
    assert _ids(curate_voice_catalog(catalog, {}, search="female")) == ["en-us-amy"]
    # by source connection
    assert _ids(curate_voice_catalog(catalog, {}, search="home")) == ["de-de-max"]


@pytest.mark.tier0
def test_curate_empty_search_returns_all():
    """AC3: an empty/whitespace search does not filter — the full list stands."""
    catalog = _agg_catalog(("a", "c1"), ("b", "c2"))
    assert _ids(curate_voice_catalog(catalog, {}, search="   ")) == ["a", "b"]
    assert _ids(curate_voice_catalog(catalog, {}, search=None)) == ["a", "b"]


@pytest.mark.tier0
def test_curate_search_survivors_keep_enabled_and_source():
    """AC3+AC1: a searched-down entry keeps its enabled flag and source."""
    catalog = _agg_catalog(("a", "studio", "Amy"), ("b", "home", "Ryan"))
    curated = curate_voice_catalog(catalog, {"a": True}, search="amy")
    assert curated["voices"] == [
        {"id": "a", "source_connection_id": "studio", "name": "Amy", "enabled": True}
    ]


@pytest.mark.tier0
def test_curate_preserves_source_connection_for_similarly_named():
    """R4/T-018 groundwork: same id from two sources stays distinguishable, each
    annotated with the same enabled flag (enabled is keyed by voice id)."""
    catalog = _agg_catalog(("amy", "studio", "Amy"), ("amy", "home", "Amy"))
    curated = curate_voice_catalog(catalog, {"amy": True})
    pairs = [(v["source_connection_id"], v["enabled"]) for v in curated["voices"]]
    assert pairs == [("studio", True), ("home", True)]


@pytest.mark.tier0
def test_curate_reflects_toggle_both_directions():
    """AC4 (pure level): flipping the map flips the reported enabled flag, while
    the voice stays listed either way — the reload-visible state."""
    catalog = _agg_catalog(("a", "c1"), ("b", "c1"))
    # Enable a, leave b off.
    curated = curate_voice_catalog(catalog, {"a": True, "b": False})
    assert {v["id"]: v["enabled"] for v in curated["voices"]} == {"a": True, "b": False}
    # A later map (as if reloaded after a toggle) flips b on, a off — both still listed.
    curated = curate_voice_catalog(catalog, {"a": False, "b": True})
    assert {v["id"]: v["enabled"] for v in curated["voices"]} == {"a": False, "b": True}


@pytest.mark.tier0
def test_curate_does_not_mutate_input():
    """The overlay is pure — the source catalog entries gain no enabled key."""
    catalog = _agg_catalog(("a", "c1"))
    curate_voice_catalog(catalog, {"a": True})
    assert catalog["voices"] == [{"id": "a", "source_connection_id": "c1"}]
    assert "enabled" not in catalog["voices"][0]


@pytest.mark.tier0
def test_curate_tolerates_non_dict_map():
    """A malformed (non-dict) enabled map degrades to nothing enabled, still lists."""
    catalog = _agg_catalog(("a", "c1"), ("b", "c1"))
    curated = curate_voice_catalog(catalog, None)
    assert _ids(curated) == ["a", "b"]
    assert all(v["enabled"] is False for v in curated["voices"])


@pytest.mark.tier0
def test_curate_empty_catalog_stays_empty():
    """Curating an empty catalog yields an empty curation surface."""
    assert curate_voice_catalog({"voices": []}, {"a": True}) == {"voices": []}
    assert curate_voice_catalog({}, {"a": True}) == {"voices": []}


# ---------------------------------------------------------------------------
# R4 (cavekit-audio-voice-picker): source connection shown in curation (T-018)
#   AC1 — each voice in the curation list displays its source connection.
#   AC2 — two similarly-named voices from different connections are
#         distinguishable by their SHOWN source.
# ---------------------------------------------------------------------------


def _tts_conn(cid, **fields):
    """A saved self-hosted TTS connection with the given id and field values."""
    return SavedAudioConnection(
        id=cid, type=AudioConnectionType.SELF_HOSTED_TTS, fields=dict(fields)
    )


@pytest.mark.tier0
def test_connection_display_label_uses_type_label_and_model():
    """A connection's human label is its type label plus its distinguishing field.

    The self-hosted TTS field set names a connection by its ``model`` (speaker
    model), so the label is meaningful rather than the opaque connection id.
    """
    conn = _tts_conn("a3f9-uuid", model="glados", split_on=".")
    assert connection_display_label(conn) == "Self-hosted TTS (glados)"


@pytest.mark.tier0
def test_connection_display_label_falls_back_to_type_label():
    """With no naming field present, the bare type label is used (still human)."""
    conn = _tts_conn("bare", split_on=".")
    assert connection_display_label(conn) == "Self-hosted TTS"


@pytest.mark.tier0
def test_connection_display_label_is_not_the_opaque_id():
    """The whole point of R4: the shown source is not the raw connection id."""
    conn = _tts_conn("deadbeefcafe", model="amy-voice")
    label = connection_display_label(conn)
    assert "deadbeefcafe" not in label
    assert label == "Self-hosted TTS (amy-voice)"


@pytest.mark.tier0
def test_disambiguate_leaves_unique_labels_untouched():
    """Distinct base labels are shown verbatim — no id noise added when not needed."""
    resolved = disambiguate_source_labels(
        {"c1": "Self-hosted TTS (studio)", "c2": "Self-hosted TTS (home)"}
    )
    assert resolved == {
        "c1": "Self-hosted TTS (studio)",
        "c2": "Self-hosted TTS (home)",
    }


@pytest.mark.tier0
def test_disambiguate_makes_colliding_labels_distinct():
    """AC2: two connections with the SAME base label get a per-connection distinct
    shown source (the id is appended only to the colliding ones)."""
    resolved = disambiguate_source_labels(
        {"c1": "Self-hosted TTS (m)", "c2": "Self-hosted TTS (m)"}
    )
    assert resolved["c1"] != resolved["c2"]
    assert resolved["c1"] == "Self-hosted TTS (m) #c1"
    assert resolved["c2"] == "Self-hosted TTS (m) #c2"


@pytest.mark.tier0
def test_disambiguate_non_dict_is_empty():
    """A malformed (non-dict) label map degrades to an empty map, not a raise."""
    assert disambiguate_source_labels(None) == {}
    assert disambiguate_source_labels(["not", "a", "dict"]) == {}


@pytest.mark.tier0
def test_label_voice_sources_attaches_shown_label():
    """AC1: every voice gains a human-readable source_connection_label for display."""
    catalog = {
        "voices": [
            {"id": "amy", "source_connection_id": "c1"},
            {"id": "nova", "source_connection_id": "c2"},
        ]
    }
    labelled = label_voice_sources(
        catalog, {"c1": "Self-hosted TTS (studio)", "c2": "Self-hosted TTS (home)"}
    )
    shown = {v["id"]: v["source_connection_label"] for v in labelled["voices"]}
    assert shown == {"amy": "Self-hosted TTS (studio)", "nova": "Self-hosted TTS (home)"}
    # The authoritative id is preserved untouched alongside the display label.
    assert [v["source_connection_id"] for v in labelled["voices"]] == ["c1", "c2"]


@pytest.mark.tier0
def test_label_voice_sources_similarly_named_distinguishable_by_shown_source():
    """AC2: two same-name voices from different connections show different sources.

    End-to-end at the pure level: two connections with an identical base label are
    disambiguated, then the labels are attached — so the two "Amy" voices are
    distinguishable by their SHOWN source, not merely by the opaque id.
    """
    catalog = {
        "voices": [
            {"id": "amy", "name": "Amy", "source_connection_id": "c1"},
            {"id": "amy", "name": "Amy", "source_connection_id": "c2"},
        ]
    }
    base = {"c1": "Self-hosted TTS (m)", "c2": "Self-hosted TTS (m)"}
    labelled = label_voice_sources(catalog, disambiguate_source_labels(base))
    shown = [v["source_connection_label"] for v in labelled["voices"]]
    # Both are named "Amy" yet show two distinct sources.
    assert len({v["name"] for v in labelled["voices"]}) == 1
    assert shown[0] != shown[1]
    assert shown == ["Self-hosted TTS (m) #c1", "Self-hosted TTS (m) #c2"]


@pytest.mark.tier0
def test_label_voice_sources_falls_back_to_id_when_unknown():
    """A source id absent from the label map shows the bare id (never blank), so a
    voice always displays *some* source and stays distinguishable (AC1/AC2)."""
    catalog = {"voices": [{"id": "x", "source_connection_id": "orphan"}]}
    labelled = label_voice_sources(catalog, {})
    assert labelled["voices"][0]["source_connection_label"] == "orphan"


@pytest.mark.tier0
def test_label_voice_sources_does_not_mutate_input():
    """The overlay is pure — the source catalog entries gain no label key."""
    catalog = {"voices": [{"id": "x", "source_connection_id": "c1"}]}
    label_voice_sources(catalog, {"c1": "L"})
    assert catalog["voices"][0] == {"id": "x", "source_connection_id": "c1"}
    assert "source_connection_label" not in catalog["voices"][0]


@pytest.mark.tier0
def test_label_voice_sources_empty_catalog_stays_empty():
    """Labelling an empty or malformed catalog yields an empty result."""
    assert label_voice_sources({"voices": []}, {"c1": "L"}) == {"voices": []}
    assert label_voice_sources({}, {"c1": "L"}) == {"voices": []}
    assert label_voice_sources(None, {"c1": "L"}) == {"voices": []}
