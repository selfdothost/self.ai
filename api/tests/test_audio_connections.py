"""cavekit-audio-connections.md R1 (Typed Connection Creation) — the type-first
chooser flow.

Verifies each R1 acceptance criterion individually:
  AC1: initiating a new connection presents a choice of the five types before any
       type-specific field is shown.
  AC2: no type-specific field is editable until a type has been chosen.
  AC3: each of the five types is individually selectable.
"""

import pytest

from selfai_ui.audio.connections import (
    TYPE_FIELD_NAMES,
    AudioConnectionType,
    TypeNotChosenError,
    available_connection_types,
    choose_type,
    get_type_info,
    new_audio_connection_draft,
)

ALL_FIVE = [
    AudioConnectionType.SELF_HOSTED_STT,
    AudioConnectionType.SELF_HOSTED_TTS,
    AudioConnectionType.HOSTED_GENERAL,
    AudioConnectionType.HOSTED_SPECIALIZED_A,
    AudioConnectionType.HOSTED_SPECIALIZED_B,
]


# --- AC1: choice of five types presented before any type-specific field --------


@pytest.mark.tier0
def test_exactly_five_connection_types_are_offered():
    infos = available_connection_types()
    assert len(infos) == 5
    assert {info.type for info in infos} == set(ALL_FIVE)


@pytest.mark.tier0
def test_the_five_types_are_the_kit_specified_kinds():
    infos = {info.type: info for info in available_connection_types()}

    # two self-hosted
    assert infos[AudioConnectionType.SELF_HOSTED_STT].self_hosted is True
    assert infos[AudioConnectionType.SELF_HOSTED_STT].capabilities == ("stt",)
    assert infos[AudioConnectionType.SELF_HOSTED_TTS].self_hosted is True
    assert infos[AudioConnectionType.SELF_HOSTED_TTS].capabilities == ("tts",)

    # three hosted third-party
    assert infos[AudioConnectionType.HOSTED_GENERAL].self_hosted is False
    assert infos[AudioConnectionType.HOSTED_SPECIALIZED_A].self_hosted is False
    assert infos[AudioConnectionType.HOSTED_SPECIALIZED_B].self_hosted is False


@pytest.mark.tier0
def test_new_draft_shows_no_type_specific_field_before_a_choice():
    # AC1: the chooser is presented, but before choosing, no type-specific field
    # is shown/editable.
    draft = new_audio_connection_draft()
    assert draft.type_chosen is False
    assert draft.editable_fields() == ()
    assert draft.fields == {}


# --- AC2: no type-specific field editable until a type is chosen ---------------


@pytest.mark.tier0
def test_setting_a_field_before_choosing_a_type_is_refused():
    draft = new_audio_connection_draft()
    with pytest.raises(TypeNotChosenError):
        draft.set_field("url", "http://self.transcribe:8000")
    assert draft.fields == {}


@pytest.mark.tier0
def test_fields_become_editable_only_after_choosing_a_type():
    draft = new_audio_connection_draft()
    assert draft.editable_fields() == ()  # locked

    choose_type(draft, AudioConnectionType.HOSTED_GENERAL)

    assert draft.editable_fields() == TYPE_FIELD_NAMES[AudioConnectionType.HOSTED_GENERAL]
    draft.set_field("tts_base_url", "https://api.openai.com/v1")  # now allowed
    assert draft.fields["tts_base_url"] == "https://api.openai.com/v1"


@pytest.mark.tier0
def test_chosen_type_exposes_only_its_own_fields():
    # A field belonging to a different type is not editable on this draft.
    draft = new_audio_connection_draft()
    choose_type(draft, AudioConnectionType.HOSTED_SPECIALIZED_A)  # TTS-only: api_key/model/voice/split_on

    assert "region" not in draft.editable_fields()  # region belongs to type B (Azure)
    with pytest.raises(KeyError):
        draft.set_field("region", "eastus")


# --- AC3: each of the five types is individually selectable --------------------


@pytest.mark.tier0
@pytest.mark.parametrize("connection_type", ALL_FIVE)
def test_each_type_is_individually_selectable(connection_type):
    draft = new_audio_connection_draft()
    returned = choose_type(draft, connection_type)

    assert returned.type is connection_type
    assert returned.type_chosen is True
    # every type unlocks a non-empty, type-scoped field set
    assert returned.editable_fields() == TYPE_FIELD_NAMES[connection_type]
    assert len(returned.editable_fields()) >= 1


@pytest.mark.tier0
@pytest.mark.parametrize("connection_type", ALL_FIVE)
def test_every_type_has_a_distinct_stable_wire_identifier(connection_type):
    # str-valued enum: the value is a stable identifier usable on the wire.
    info = get_type_info(connection_type)
    assert info.type.value == connection_type.value
    assert isinstance(connection_type.value, str) and connection_type.value


@pytest.mark.tier0
def test_choosing_type_rejects_a_non_type_value():
    draft = new_audio_connection_draft()
    with pytest.raises(TypeError):
        choose_type(draft, "openai")  # a legacy engine string, not a type


@pytest.mark.tier0
def test_switching_type_rescopes_the_field_set():
    draft = new_audio_connection_draft()
    choose_type(draft, AudioConnectionType.HOSTED_SPECIALIZED_B)  # Azure: has a region field
    draft.set_field("region", "eastus")
    assert draft.fields == {"region": "eastus"}

    # switch before save -> field set tracks the new type, stale fields cleared
    choose_type(draft, AudioConnectionType.SELF_HOSTED_STT)  # base_url/control_base_url/model
    assert draft.fields == {}
    assert draft.editable_fields() == TYPE_FIELD_NAMES[AudioConnectionType.SELF_HOSTED_STT]


@pytest.mark.tier0
def test_isinstance_of_str_enum_helps_wire_use():
    assert isinstance(AudioConnectionType.HOSTED_GENERAL, str)
    assert AudioConnectionType("hosted_general") is AudioConnectionType.HOSTED_GENERAL
