"""Unit tests for hosted-provider real voice catalogs (T-013).

Covers cavekit-audio-voice-catalog R4 (Hosted-Provider Catalogs Are Real Too)
at the pure-function level — the mirror of ``test_voice_catalog.py``'s R1 tests,
but for the three HOSTED provider types:

  AC1 — a hosted-provider connection returns that provider's actual voice list,
        not a placeholder.
  AC2 — the fetch strategy for a connection's catalog is selected from the
        connection's declared type, not inferred heuristically.
  AC3 — each of the three hosted-provider types resolves to its own provider's
        real voice list.

The pure normalizers take an already-fetched raw provider response (exactly as
``build_voice_catalog`` does for the self-hosted path), so the strategy-by-type
selection and the per-provider parse are directly testable without any network.
"""

import pytest

from selfai_ui.audio.connections import AudioConnectionType
from selfai_ui.audio.voice_catalog import (
    _normalize_azure,
    _normalize_elevenlabs,
    _normalize_openai,
    build_hosted_voice_catalog,
    hosted_fetch_strategy,
    is_hosted_type,
)


def _ids(catalog):
    return [v["id"] for v in catalog["voices"]]


def _by_id(catalog, voice_id):
    return next(v for v in catalog["voices"] if v["id"] == voice_id)


# --- Realistic per-provider raw responses -----------------------------------

# OpenAI has no live listing endpoint; a failed/absent fetch is None.
_OPENAI_RAW = None

# ElevenLabs: voices wrapped under "voices", gender nested under "labels".
_ELEVENLABS_RAW = {
    "voices": [
        {
            "voice_id": "21m00Tcm4TlvDq8ikWAM",
            "name": "Rachel",
            "labels": {"gender": "female", "accent": "american"},
        },
        {
            "voice_id": "AZnzlk1XvdvUeBnXmlld",
            "name": "Domi",
            "labels": {"gender": "female"},
            "language": "en",
        },
    ]
}

# Azure: flat list of capitalized-key rows.
_AZURE_RAW = [
    {
        "ShortName": "en-US-JennyNeural",
        "DisplayName": "Jenny",
        "Locale": "en-US",
        "Gender": "Female",
    },
    {
        "ShortName": "de-DE-ConradNeural",
        "DisplayName": "Conrad",
        "Locale": "de-DE",
        "Gender": "Male",
    },
]


# --- AC2: strategy selected from declared type, not inferred -----------------


@pytest.mark.tier0
def test_strategy_selected_from_declared_type():
    """AC2: each hosted type maps to its own provider normalizer, by type alone."""
    assert hosted_fetch_strategy(AudioConnectionType.HOSTED_GENERAL) is _normalize_openai
    assert (
        hosted_fetch_strategy(AudioConnectionType.HOSTED_SPECIALIZED_A)
        is _normalize_elevenlabs
    )
    assert (
        hosted_fetch_strategy(AudioConnectionType.HOSTED_SPECIALIZED_B)
        is _normalize_azure
    )


@pytest.mark.tier0
def test_self_hosted_types_have_no_hosted_strategy():
    """AC2: the two self-hosted types are not routed through the hosted dispatch.

    They take the T-010 ``build_voice_catalog`` path; asking for a hosted strategy
    raises rather than silently falling back to a default provider.
    """
    for self_hosted in (
        AudioConnectionType.SELF_HOSTED_STT,
        AudioConnectionType.SELF_HOSTED_TTS,
    ):
        assert not is_hosted_type(self_hosted)
        with pytest.raises(ValueError):
            hosted_fetch_strategy(self_hosted)
        with pytest.raises(ValueError):
            build_hosted_voice_catalog(self_hosted, _AZURE_RAW)


@pytest.mark.tier0
def test_unknown_type_raises_no_heuristic_fallback():
    """AC2: an unknown / non-enum type never falls through to a guessed provider."""
    for bad in (None, "openai", "elevenlabs", 42, object()):
        assert not is_hosted_type(bad)
        with pytest.raises(ValueError):
            hosted_fetch_strategy(bad)


@pytest.mark.tier0
def test_selection_ignores_response_body():
    """AC2: strategy selection depends only on the type, never on the payload.

    The same Azure-shaped payload, dispatched under the ElevenLabs type, is parsed
    by the ElevenLabs normalizer (yielding nothing, since Azure rows carry no
    ``voice_id``) — proving the response body does not steer strategy choice.
    """
    catalog = build_hosted_voice_catalog(
        AudioConnectionType.HOSTED_SPECIALIZED_A, _AZURE_RAW
    )
    assert catalog == {"voices": []}


# --- AC1 / AC3: each provider returns its own real list ----------------------


@pytest.mark.tier0
def test_openai_returns_provider_real_fixed_voices():
    """AC1/AC3: HOSTED_GENERAL yields OpenAI's real published voice set."""
    catalog = build_hosted_voice_catalog(
        AudioConnectionType.HOSTED_GENERAL, _OPENAI_RAW
    )
    assert _ids(catalog) == ["alloy", "echo", "fable", "onyx", "nova", "shimmer"]
    # A real, human-readable name accompanies each — never a bare id-only entry.
    assert _by_id(catalog, "alloy")["name"] == "Alloy"


@pytest.mark.tier0
def test_openai_honors_live_list_when_present():
    """A compatible gateway that DOES return a list is honored over the fixed set."""
    catalog = build_hosted_voice_catalog(
        AudioConnectionType.HOSTED_GENERAL,
        {"data": [{"id": "custom-voice", "name": "Custom"}]},
    )
    assert _ids(catalog) == ["custom-voice"]


@pytest.mark.tier0
def test_elevenlabs_returns_provider_real_voices():
    """AC1/AC3: HOSTED_SPECIALIZED_A yields ElevenLabs' actual voices + metadata."""
    catalog = build_hosted_voice_catalog(
        AudioConnectionType.HOSTED_SPECIALIZED_A, _ELEVENLABS_RAW
    )
    assert _ids(catalog) == ["21m00Tcm4TlvDq8ikWAM", "AZnzlk1XvdvUeBnXmlld"]
    rachel = _by_id(catalog, "21m00Tcm4TlvDq8ikWAM")
    assert rachel["name"] == "Rachel"
    # Gender is read from the nested ``labels`` block.
    assert rachel["gender"] == "female"
    domi = _by_id(catalog, "AZnzlk1XvdvUeBnXmlld")
    assert domi["language"] == "en"


@pytest.mark.tier0
def test_azure_returns_provider_real_voices():
    """AC1/AC3: HOSTED_SPECIALIZED_B yields Azure's actual region voice list."""
    catalog = build_hosted_voice_catalog(
        AudioConnectionType.HOSTED_SPECIALIZED_B, _AZURE_RAW
    )
    assert _ids(catalog) == ["en-US-JennyNeural", "de-DE-ConradNeural"]
    jenny = _by_id(catalog, "en-US-JennyNeural")
    # Name mirrors the prior azure branch: "DisplayName (ShortName)".
    assert jenny["name"] == "Jenny (en-US-JennyNeural)"
    assert jenny["language"] == "en-US"
    assert jenny["gender"] == "Female"


@pytest.mark.tier0
def test_three_hosted_types_yield_disjoint_real_lists():
    """AC3: the three hosted types resolve to three different real voice lists.

    No shared placeholder set is returned by any two of them.
    """
    openai_ids = set(
        _ids(build_hosted_voice_catalog(AudioConnectionType.HOSTED_GENERAL, _OPENAI_RAW))
    )
    eleven_ids = set(
        _ids(
            build_hosted_voice_catalog(
                AudioConnectionType.HOSTED_SPECIALIZED_A, _ELEVENLABS_RAW
            )
        )
    )
    azure_ids = set(
        _ids(
            build_hosted_voice_catalog(
                AudioConnectionType.HOSTED_SPECIALIZED_B, _AZURE_RAW
            )
        )
    )
    assert openai_ids and eleven_ids and azure_ids
    assert openai_ids.isdisjoint(eleven_ids)
    assert openai_ids.isdisjoint(azure_ids)
    assert eleven_ids.isdisjoint(azure_ids)


# --- AC1: no fabrication; contract shape parity ------------------------------


@pytest.mark.tier0
def test_absent_provider_response_yields_empty_not_placeholder():
    """AC1: a provider that returns nothing yields an empty catalog (not a set).

    Applies to the providers that DO have a listing endpoint — a failed fetch
    (None) or empty list must not fabricate voices. (OpenAI is exempt: it has no
    listing endpoint, so its fixed published set is its real catalog.)
    """
    for hosted in (
        AudioConnectionType.HOSTED_SPECIALIZED_A,
        AudioConnectionType.HOSTED_SPECIALIZED_B,
    ):
        assert build_hosted_voice_catalog(hosted, None) == {"voices": []}
        assert build_hosted_voice_catalog(hosted, []) == {"voices": []}
        assert build_hosted_voice_catalog(hosted, {"voices": []}) == {"voices": []}


@pytest.mark.tier0
def test_absent_voice_not_synthesized():
    """AC1: a voice the provider never reported never appears in the catalog."""
    catalog = build_hosted_voice_catalog(
        AudioConnectionType.HOSTED_SPECIALIZED_A, _ELEVENLABS_RAW
    )
    assert "not-a-real-voice" not in _ids(catalog)


@pytest.mark.tier0
def test_every_hosted_entry_carries_all_metadata_keys():
    """R2 parity: every hosted entry exposes id/name/language/gender, name non-empty."""
    for conn_type, raw in (
        (AudioConnectionType.HOSTED_GENERAL, _OPENAI_RAW),
        (AudioConnectionType.HOSTED_SPECIALIZED_A, _ELEVENLABS_RAW),
        (AudioConnectionType.HOSTED_SPECIALIZED_B, _AZURE_RAW),
    ):
        catalog = build_hosted_voice_catalog(conn_type, raw)
        assert catalog["voices"], f"{conn_type} produced no voices"
        for entry in catalog["voices"]:
            assert set(entry.keys()) == {"id", "name", "language", "gender"}
            assert isinstance(entry["name"], str) and entry["name"]


@pytest.mark.tier0
def test_hosted_catalog_deduplicates_by_id():
    """Repeated ids collapse to one, first-seen order preserved (contract parity)."""
    raw = {
        "voices": [
            {"voice_id": "dup", "name": "First"},
            {"voice_id": "other", "name": "Other"},
            {"voice_id": "dup", "name": "Second"},
        ]
    }
    catalog = build_hosted_voice_catalog(AudioConnectionType.HOSTED_SPECIALIZED_A, raw)
    assert _ids(catalog) == ["dup", "other"]
    assert _by_id(catalog, "dup")["name"] == "First"


@pytest.mark.tier0
def test_elevenlabs_name_falls_back_to_id():
    """R2 AC4: an ElevenLabs voice with no name still carries a name (its id)."""
    raw = {"voices": [{"voice_id": "vid-only"}]}
    entry = _by_id(
        build_hosted_voice_catalog(AudioConnectionType.HOSTED_SPECIALIZED_A, raw),
        "vid-only",
    )
    assert entry["name"] == "vid-only"


@pytest.mark.tier0
def test_azure_row_without_shortname_skipped():
    """AC1: an Azure row lacking a ShortName is skipped, not fabricated."""
    raw = [{"DisplayName": "Nameless", "Locale": "en-US"}, {"ShortName": "en-US-X"}]
    catalog = build_hosted_voice_catalog(AudioConnectionType.HOSTED_SPECIALIZED_B, raw)
    assert _ids(catalog) == ["en-US-X"]
