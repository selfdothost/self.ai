"""cavekit-audio-connections.md R3 (Type-Appropriate Fields).

Verifies each R3 acceptance criterion individually:
  AC1: selecting a given type presents only that type's applicable fields.
  AC2: a field not used by the selected type is absent from that type's form
       (not merely disabled or ignored).
  AC3: switching the chosen type before saving updates the presented field set
       to match the newly chosen type.

Plus the relocation-fidelity guard: the per-type field sets are inherited
UNCHANGED from the prior settings-tab arrangement — every field traces to an
existing ``config.py`` PersistentConfig, and the only omitted knob is ``ENGINE``
(now expressed by the connection type itself).
"""

import pytest

from selfai_ui.audio.connections import (
    TYPE_FIELD_NAMES,
    TYPE_FIELDS,
    AudioConnectionField,
    AudioConnectionType,
    choose_type,
    fields_for,
    new_audio_connection_draft,
)

ALL_FIVE = [
    AudioConnectionType.SELF_HOSTED_STT,
    AudioConnectionType.SELF_HOSTED_TTS,
    AudioConnectionType.HOSTED_GENERAL,
    AudioConnectionType.HOSTED_SPECIALIZED_A,
    AudioConnectionType.HOSTED_SPECIALIZED_B,
]

# The authoritative expectation, restated independently of the module under test:
# each type's field-name set inherited from the prior settings-tab arrangement.
EXPECTED_FIELD_NAMES = {
    AudioConnectionType.SELF_HOSTED_STT: ("base_url", "control_base_url", "model"),
    AudioConnectionType.SELF_HOSTED_TTS: ("model", "split_on"),
    AudioConnectionType.HOSTED_GENERAL: (
        "stt_base_url",
        "stt_api_key",
        "stt_model",
        "tts_base_url",
        "tts_api_key",
        "tts_model",
        "tts_voice",
        "split_on",
    ),
    AudioConnectionType.HOSTED_SPECIALIZED_A: ("api_key", "model", "voice", "split_on"),
    AudioConnectionType.HOSTED_SPECIALIZED_B: (
        "api_key",
        "region",
        "voice",
        "output_format",
        "split_on",
    ),
}


# --- AC1: selecting a type presents only that type's applicable fields ---------


@pytest.mark.tier0
@pytest.mark.parametrize("connection_type", ALL_FIVE)
def test_selecting_a_type_presents_exactly_its_fields(connection_type):
    draft = new_audio_connection_draft()
    choose_type(draft, connection_type)

    assert draft.editable_fields() == EXPECTED_FIELD_NAMES[connection_type]
    assert TYPE_FIELD_NAMES[connection_type] == EXPECTED_FIELD_NAMES[connection_type]


@pytest.mark.tier0
def test_every_type_has_a_nonempty_field_set():
    for connection_type in ALL_FIVE:
        assert len(fields_for(connection_type)) >= 1


# --- AC2: a field a type does not use is absent (not disabled/ignored) ---------


@pytest.mark.tier0
def test_unused_field_is_absent_not_merely_disabled():
    # `region` is an Azure-only field; it must not appear at all on any other
    # type's form — absence, not a disabled control.
    for connection_type in ALL_FIVE:
        names = TYPE_FIELD_NAMES[connection_type]
        if connection_type is AudioConnectionType.HOSTED_SPECIALIZED_B:
            assert "region" in names
        else:
            assert "region" not in names


@pytest.mark.tier0
def test_self_hosted_types_carry_no_api_key_field():
    # Self-hosted STT/TTS are perimeter-trusted (cavekit R8) and read no key in
    # routers/audio.py — no credential field should be present.
    for connection_type in (
        AudioConnectionType.SELF_HOSTED_STT,
        AudioConnectionType.SELF_HOSTED_TTS,
    ):
        for field in fields_for(connection_type):
            assert not field.secret
            assert "api_key" not in field.name


@pytest.mark.tier0
def test_control_url_is_unique_to_self_hosted_stt():
    # The management control port (T-020) is meaningful only for self-hosted STT.
    for connection_type in ALL_FIVE:
        names = TYPE_FIELD_NAMES[connection_type]
        if connection_type is AudioConnectionType.SELF_HOSTED_STT:
            assert "control_base_url" in names
        else:
            assert "control_base_url" not in names


@pytest.mark.tier0
def test_a_draft_refuses_a_field_foreign_to_its_type():
    draft = new_audio_connection_draft()
    choose_type(draft, AudioConnectionType.SELF_HOSTED_TTS)  # model, split_on
    with pytest.raises(KeyError):
        draft.set_field("region", "eastus")  # Azure-only field


# --- AC3: switching type before saving updates the presented field set --------


@pytest.mark.tier0
def test_switching_type_updates_the_presented_field_set():
    draft = new_audio_connection_draft()

    choose_type(draft, AudioConnectionType.HOSTED_SPECIALIZED_B)  # Azure
    assert draft.editable_fields() == EXPECTED_FIELD_NAMES[
        AudioConnectionType.HOSTED_SPECIALIZED_B
    ]
    draft.set_field("region", "eastus")
    draft.set_field("output_format", "audio-24khz-160kbitrate-mono-mp3")

    # switch, before saving, to a type that shares none of those fields
    choose_type(draft, AudioConnectionType.SELF_HOSTED_STT)
    assert draft.editable_fields() == EXPECTED_FIELD_NAMES[
        AudioConnectionType.SELF_HOSTED_STT
    ]
    # stale fields from the previous type are gone, not carried over
    assert draft.fields == {}
    assert "region" not in draft.editable_fields()
    assert "output_format" not in draft.editable_fields()


@pytest.mark.tier0
@pytest.mark.parametrize("first", ALL_FIVE)
@pytest.mark.parametrize("second", ALL_FIVE)
def test_switching_between_any_two_types_tracks_the_field_set(first, second):
    draft = new_audio_connection_draft()
    choose_type(draft, first)
    assert draft.editable_fields() == EXPECTED_FIELD_NAMES[first]
    choose_type(draft, second)
    assert draft.editable_fields() == EXPECTED_FIELD_NAMES[second]


# --- Relocation fidelity: fields inherited unchanged from the prior tab --------


@pytest.mark.tier0
@pytest.mark.parametrize("connection_type", ALL_FIVE)
def test_every_field_is_a_descriptor_with_a_legacy_config_anchor(connection_type):
    for field in fields_for(connection_type):
        assert isinstance(field, AudioConnectionField)
        # each relocated field names the exact config.py knob it carries over,
        # and that knob is an AUDIO_* / WHISPER_* PersistentConfig from the prior
        # settings-tab arrangement.
        assert field.legacy_config.startswith(("AUDIO_", "WHISPER_"))
        assert field.name and field.label


@pytest.mark.tier0
def test_engine_knob_is_the_only_omission():
    # The prior tab's ENGINE selector is not relocated as a field — the type IS
    # the engine. No relocated field should reference an *_ENGINE config.
    for connection_type in ALL_FIVE:
        for field in fields_for(connection_type):
            assert not field.legacy_config.endswith("_ENGINE")


@pytest.mark.tier0
def test_split_on_is_shared_by_every_tts_capable_type():
    # SPLIT_ON is a TTS-wide knob in the prior arrangement; every TTS-capable
    # type carries it, and the STT-only self-hosted type does not.
    tts_types = (
        AudioConnectionType.SELF_HOSTED_TTS,
        AudioConnectionType.HOSTED_GENERAL,
        AudioConnectionType.HOSTED_SPECIALIZED_A,
        AudioConnectionType.HOSTED_SPECIALIZED_B,
    )
    for connection_type in tts_types:
        assert "split_on" in TYPE_FIELD_NAMES[connection_type]
    assert "split_on" not in TYPE_FIELD_NAMES[AudioConnectionType.SELF_HOSTED_STT]


@pytest.mark.tier0
def test_field_names_are_unique_within_each_type():
    for connection_type in ALL_FIVE:
        names = TYPE_FIELD_NAMES[connection_type]
        assert len(names) == len(set(names))


@pytest.mark.tier0
def test_type_fields_and_names_stay_in_lockstep():
    assert set(TYPE_FIELDS) == set(ALL_FIVE)
    assert set(TYPE_FIELD_NAMES) == set(ALL_FIVE)
    for connection_type in ALL_FIVE:
        assert TYPE_FIELD_NAMES[connection_type] == tuple(
            f.name for f in TYPE_FIELDS[connection_type]
        )
