"""cavekit-audio-connections.md R2 (Multiple Connections Per Type) — persistence.

Verifies each R2 acceptance criterion individually against
``selfai_ui.audio.connections.AudioConnectionStore``:
  AC1: two connections of the same type can both be saved and both persist.
  AC2: each connection of a shared type is independently addressable.
  AC3: editing one connection of a shared type does not affect another.
  AC4: deleting one connection of a shared type does not affect another.
  AC5: the saved set survives a reload with no connection collapsed/overwritten.
"""

import itertools

import pytest

from selfai_ui.audio.connections import (
    AudioConnectionStore,
    AudioConnectionType,
    ConnectionNotFoundError,
    SavedAudioConnection,
    TypeNotChosenError,
    UnknownFieldError,
    choose_type,
    new_audio_connection_draft,
)


def _counting_id_factory():
    """Deterministic, monotonically-increasing ids for reproducible tests."""

    counter = itertools.count(1)
    return lambda: f"conn-{next(counter)}"


# --- AC1: two connections of the same type both save and both persist ----------


@pytest.mark.tier0
def test_two_connections_of_the_same_type_both_persist():
    store = AudioConnectionStore(id_factory=_counting_id_factory())

    a = store.add(AudioConnectionType.SELF_HOSTED_STT, {"base_url": "http://a:8000"})
    b = store.add(AudioConnectionType.SELF_HOSTED_STT, {"base_url": "http://b:8000"})

    assert a.id != b.id
    saved = store.list()
    assert len(saved) == 2
    assert {c.id for c in saved} == {a.id, b.id}
    # both are the same type, and neither collapsed into the other
    assert [c.type for c in saved] == [
        AudioConnectionType.SELF_HOSTED_STT,
        AudioConnectionType.SELF_HOSTED_STT,
    ]
    assert store.list_by_type(AudioConnectionType.SELF_HOSTED_STT) == [a, b]


@pytest.mark.tier0
def test_two_same_type_connections_with_identical_fields_do_not_merge():
    # Even byte-identical fields must not be de-duplicated into one.
    store = AudioConnectionStore(id_factory=_counting_id_factory())
    a = store.add(AudioConnectionType.HOSTED_SPECIALIZED_A, {"api_key": "same"})
    b = store.add(AudioConnectionType.HOSTED_SPECIALIZED_A, {"api_key": "same"})

    assert a.id != b.id
    assert len(store) == 2


@pytest.mark.tier0
def test_saving_a_draft_from_the_type_first_flow_persists_it():
    store = AudioConnectionStore(id_factory=_counting_id_factory())
    draft = new_audio_connection_draft()
    choose_type(draft, AudioConnectionType.HOSTED_GENERAL)
    draft.set_field("tts_base_url", "https://api.openai.com/v1")
    draft.set_field("tts_api_key", "sk-1")

    saved = store.save_draft(draft)

    assert saved.type is AudioConnectionType.HOSTED_GENERAL
    assert saved.fields == {"tts_base_url": "https://api.openai.com/v1", "tts_api_key": "sk-1"}
    assert store.get(saved.id) is saved


@pytest.mark.tier0
def test_saving_a_draft_without_a_chosen_type_is_refused():
    store = AudioConnectionStore()
    draft = new_audio_connection_draft()
    with pytest.raises(TypeNotChosenError):
        store.save_draft(draft)
    assert store.list() == []


# --- AC2: each connection of a shared type is independently addressable ---------


@pytest.mark.tier0
def test_each_same_type_connection_is_addressable_by_its_own_id():
    store = AudioConnectionStore(id_factory=_counting_id_factory())
    a = store.add(AudioConnectionType.SELF_HOSTED_TTS, {"model": "voice-a"})
    b = store.add(AudioConnectionType.SELF_HOSTED_TTS, {"model": "voice-b"})

    assert store.get(a.id).fields == {"model": "voice-a"}
    assert store.get(b.id).fields == {"model": "voice-b"}
    assert store.get(a.id) is not store.get(b.id)


@pytest.mark.tier0
def test_getting_an_unknown_id_raises():
    store = AudioConnectionStore()
    with pytest.raises(ConnectionNotFoundError):
        store.get("nope")


@pytest.mark.tier0
def test_caller_field_dict_does_not_bleed_into_the_store():
    store = AudioConnectionStore(id_factory=_counting_id_factory())
    src = {"base_url": "http://a"}
    saved = store.add(AudioConnectionType.SELF_HOSTED_STT, src)
    src["base_url"] = "http://mutated"  # mutate caller's dict afterwards
    assert store.get(saved.id).fields == {"base_url": "http://a"}


# --- AC3: editing one shared-type connection does not affect another -----------


@pytest.mark.tier0
def test_editing_one_connection_leaves_a_same_type_sibling_untouched():
    store = AudioConnectionStore(id_factory=_counting_id_factory())
    a = store.add(AudioConnectionType.SELF_HOSTED_STT, {"base_url": "http://a"})
    b = store.add(AudioConnectionType.SELF_HOSTED_STT, {"base_url": "http://b"})

    store.update_fields(a.id, {"base_url": "http://a-edited"})

    assert store.get(a.id).fields == {"base_url": "http://a-edited"}
    assert store.get(b.id).fields == {"base_url": "http://b"}  # sibling untouched


@pytest.mark.tier0
def test_editing_rejects_a_field_not_belonging_to_the_type():
    store = AudioConnectionStore(id_factory=_counting_id_factory())
    a = store.add(AudioConnectionType.HOSTED_SPECIALIZED_A, {"api_key": "k"})
    with pytest.raises(UnknownFieldError):
        store.update_fields(a.id, {"region": "eastus"})  # region belongs to type B
    assert store.get(a.id).fields == {"api_key": "k"}


@pytest.mark.tier0
def test_adding_rejects_a_field_not_belonging_to_the_type():
    store = AudioConnectionStore()
    with pytest.raises(UnknownFieldError):
        store.add(AudioConnectionType.SELF_HOSTED_STT, {"api_key": "nope"})


# --- AC4: deleting one shared-type connection does not affect another -----------


@pytest.mark.tier0
def test_deleting_one_connection_leaves_a_same_type_sibling_intact():
    store = AudioConnectionStore(id_factory=_counting_id_factory())
    a = store.add(AudioConnectionType.SELF_HOSTED_TTS, {"model": "voice-a"})
    b = store.add(AudioConnectionType.SELF_HOSTED_TTS, {"model": "voice-b"})

    removed = store.delete(a.id)

    assert removed.id == a.id
    assert a.id not in store
    assert store.list() == [b]  # sibling of the same type survives
    assert store.get(b.id).fields == {"model": "voice-b"}


@pytest.mark.tier0
def test_deleting_an_unknown_id_raises():
    store = AudioConnectionStore()
    with pytest.raises(ConnectionNotFoundError):
        store.delete("nope")


# --- AC5: the saved set survives a reload, nothing collapsed/overwritten -------


@pytest.mark.tier0
def test_saved_set_survives_a_serialize_reload_round_trip():
    store = AudioConnectionStore(id_factory=_counting_id_factory())
    a = store.add(AudioConnectionType.SELF_HOSTED_STT, {"base_url": "http://a"})
    b = store.add(AudioConnectionType.SELF_HOSTED_STT, {"base_url": "http://b"})
    c = store.add(AudioConnectionType.HOSTED_SPECIALIZED_B, {"api_key": "k", "region": "eu"})

    # marshal to the config-backed form and rebuild (a "reload")
    persisted = store.to_serializable()
    reloaded = AudioConnectionStore.from_serializable(persisted)

    ids = [conn.id for conn in reloaded.list()]
    assert ids == [a.id, b.id, c.id]  # order preserved, none dropped
    # the two same-type connections both survived, unmerged and distinct
    stt = reloaded.list_by_type(AudioConnectionType.SELF_HOSTED_STT)
    assert [conn.fields["base_url"] for conn in stt] == ["http://a", "http://b"]
    # the third of a different type is intact too
    assert reloaded.get(c.id).fields == {"api_key": "k", "region": "eu"}
    assert reloaded.get(c.id).type is AudioConnectionType.HOSTED_SPECIALIZED_B


@pytest.mark.tier0
def test_serialized_form_is_keyed_by_id_with_type_and_fields_as_value():
    store = AudioConnectionStore(id_factory=_counting_id_factory())
    a = store.add(AudioConnectionType.HOSTED_GENERAL, {"tts_base_url": "u", "tts_api_key": "k"})

    persisted = store.to_serializable()
    assert persisted == {
        a.id: {"type": "hosted_general", "fields": {"tts_base_url": "u", "tts_api_key": "k"}},
    }


@pytest.mark.tier0
def test_from_serializable_tolerates_empty_and_missing_fields():
    assert AudioConnectionStore.from_serializable(None).list() == []
    assert AudioConnectionStore.from_serializable({}).list() == []
    rebuilt = AudioConnectionStore.from_serializable(
        {"x": {"type": "self_hosted_stt"}}  # no "fields" key
    )
    assert rebuilt.get("x").fields == {}


@pytest.mark.tier0
def test_reload_then_edit_still_isolates_siblings():
    # Guards AC3+AC5 together: after a reload, editing one still spares the other.
    store = AudioConnectionStore(id_factory=_counting_id_factory())
    a = store.add(AudioConnectionType.SELF_HOSTED_STT, {"base_url": "http://a"})
    b = store.add(AudioConnectionType.SELF_HOSTED_STT, {"base_url": "http://b"})

    reloaded = AudioConnectionStore.from_serializable(store.to_serializable())
    reloaded.update_fields(a.id, {"base_url": "http://a2"})

    assert reloaded.get(a.id).fields == {"base_url": "http://a2"}
    assert reloaded.get(b.id).fields == {"base_url": "http://b"}


@pytest.mark.tier0
def test_saved_connection_serializable_round_trip_is_identity():
    conn = SavedAudioConnection(
        id="k", type=AudioConnectionType.SELF_HOSTED_TTS, fields={"model": "voice-v"}
    )
    rebuilt = SavedAudioConnection.from_serializable("k", conn.to_serializable())
    assert rebuilt == conn
