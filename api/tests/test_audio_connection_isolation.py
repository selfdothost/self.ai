"""cavekit-audio-connections.md R2 — edit/delete isolation & reload-survival hardening.

T-003 owns three of R2's acceptance criteria:
  * Editing one connection of a shared type does not affect another of the same type.
  * Deleting one connection of a shared type does not affect another of the same type.
  * The saved set survives a reload of the Connections surface with no connection
    silently collapsed or overwritten by another of the same type.

T-002 built ``AudioConnectionStore`` and already covers the basic form of each
criterion (see ``test_audio_connection_persistence.py``). Investigation for T-003
found the production code correct: it is keyed by a generated id (never by
type/url), gives every connection its own ``fields`` dict, copies on add /
serialize / deserialize, and validates-before-mutating so an edit is atomic. No
production defect was found.

These tests therefore strengthen confidence by exercising the harder scenarios the
T-002 suite did not: the middle of a larger (3+) set rather than a bare pair,
multi-generation reload cycles (save -> reload -> edit -> reload -> delete ->
reload), edit-payload and reload-source aliasing in both directions, and the
atomicity of a mixed valid+foreign edit. Each maps back to one of the three
criteria above.
"""

import itertools

import pytest

from selfai_ui.audio.connections import (
    AudioConnectionStore,
    AudioConnectionType,
    UnknownFieldError,
)

STT = AudioConnectionType.SELF_HOSTED_STT


def _counting_id_factory():
    counter = itertools.count(1)
    return lambda: f"conn-{next(counter)}"


def _three_stt():
    store = AudioConnectionStore(id_factory=_counting_id_factory())
    a = store.add(STT, {"base_url": "http://a"})
    b = store.add(STT, {"base_url": "http://b"})
    c = store.add(STT, {"base_url": "http://c"})
    return store, a, b, c


# --- Criterion 1: editing one shared-type connection spares the others ---------


@pytest.mark.tier0
def test_editing_the_middle_of_three_same_type_leaves_both_flanks_untouched():
    # A bare pair can't reveal a "touches an adjacent sibling" bug; three can.
    store, a, b, c = _three_stt()

    store.update_fields(b.id, {"base_url": "http://b-edited"})

    assert store.get(b.id).fields == {"base_url": "http://b-edited"}
    assert store.get(a.id).fields == {"base_url": "http://a"}  # flank before
    assert store.get(c.id).fields == {"base_url": "http://c"}  # flank after


@pytest.mark.tier0
def test_edit_payload_dict_does_not_bleed_into_the_store():
    # update_fields must copy values in, not retain the caller's dict — otherwise a
    # later mutation of that dict would silently rewrite the stored connection.
    store, a, _b, _c = _three_stt()
    payload = {"base_url": "http://x"}

    store.update_fields(a.id, payload)
    payload["base_url"] = "http://mutated-after-the-call"

    assert store.get(a.id).fields == {"base_url": "http://x"}


@pytest.mark.tier0
def test_a_mixed_valid_and_foreign_edit_is_rejected_without_partial_write():
    # 'base_url' is valid for STT; 'region' belongs to specialized-B. The edit must
    # be atomic: rejected wholesale, with the valid half NOT applied either.
    store, a, _b, _c = _three_stt()
    before = dict(store.get(a.id).fields)

    with pytest.raises(UnknownFieldError):
        store.update_fields(a.id, {"base_url": "http://should-not-apply", "region": "eu"})

    assert store.get(a.id).fields == before


# --- Criterion 2: deleting one shared-type connection spares the others --------


@pytest.mark.tier0
def test_deleting_from_the_middle_of_a_larger_set_keeps_order_and_survivors():
    store, a, b, c = _three_stt()
    d = store.add(STT, {"base_url": "http://d"})

    store.delete(b.id)

    assert b.id not in store
    assert [x.id for x in store.list()] == [a.id, c.id, d.id]  # order preserved
    assert store.get(a.id).fields == {"base_url": "http://a"}
    assert store.get(c.id).fields == {"base_url": "http://c"}
    assert store.get(d.id).fields == {"base_url": "http://d"}


# --- Criterion 3: the saved set survives reload, nothing collapsed ------------


@pytest.mark.tier0
def test_save_reload_edit_reload_delete_reload_preserves_intent_each_generation():
    # Exercises all three criteria across three reload generations, proving edits
    # and deletes both survive round-trips and never disturb same-type siblings.
    store, a, b, c = _three_stt()

    gen1 = AudioConnectionStore.from_serializable(store.to_serializable())
    assert [x.fields["base_url"] for x in gen1.list_by_type(STT)] == [
        "http://a",
        "http://b",
        "http://c",
    ]

    gen1.update_fields(a.id, {"base_url": "http://a2"})
    gen2 = AudioConnectionStore.from_serializable(gen1.to_serializable())
    assert gen2.get(a.id).fields == {"base_url": "http://a2"}  # edit survived reload
    assert gen2.get(b.id).fields == {"base_url": "http://b"}  # siblings untouched
    assert gen2.get(c.id).fields == {"base_url": "http://c"}

    gen2.delete(b.id)
    gen3 = AudioConnectionStore.from_serializable(gen2.to_serializable())
    assert b.id not in gen3  # deletion survived reload
    assert [x.id for x in gen3.list()] == [a.id, c.id]
    assert gen3.get(a.id).fields == {"base_url": "http://a2"}


@pytest.mark.tier0
def test_reload_neither_back_aliases_into_nor_forward_aliases_from_its_source():
    store, a, _b, _c = _three_stt()
    source = store.to_serializable()

    reloaded = AudioConnectionStore.from_serializable(source)
    # editing the reloaded store must not reach back into the source payload
    reloaded.update_fields(a.id, {"base_url": "http://reloaded-edit"})
    assert source[a.id]["fields"] == {"base_url": "http://a"}

    # mutating the source payload afterwards must not reach into the reloaded store
    source[a.id]["fields"]["base_url"] = "http://source-mutated"
    assert reloaded.get(a.id).fields == {"base_url": "http://reloaded-edit"}


@pytest.mark.tier0
def test_a_serialized_snapshot_is_frozen_against_later_store_edits():
    # The payload handed to persistence must be a snapshot: editing the store after
    # serializing must not retroactively rewrite the already-serialized set.
    store, a, _b, _c = _three_stt()

    snapshot = store.to_serializable()
    store.update_fields(a.id, {"base_url": "http://after-snapshot"})

    assert snapshot[a.id]["fields"] == {"base_url": "http://a"}
