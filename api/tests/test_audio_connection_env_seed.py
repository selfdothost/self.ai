"""Guard the GitOps-declared audio-connection seed shape.

Under ENABLE_PERSISTENT_CONFIG=False the DB is ignored every boot, so typed
audio connections must be seeded from the AUDIO_CONNECTION_CONFIGS env var (a
JSON blob of the AudioConnectionStore serialized shape) declared in
manifests/api/10-deployment.yaml. This test pins that the manifest's declared
shape round-trips through AudioConnectionStore so a future change to the store
format can't silently break the manifest seed.
"""

import json

from selfai_ui.audio.connections import AudioConnectionStore, AudioConnectionType

# The exact JSON pinned in manifests/api/10-deployment.yaml
# (AUDIO_CONNECTION_CONFIGS). Keep in sync with the manifest.
MANIFEST_CONNECTION_CONFIGS = (
    '{"self-hosted-tts":{"type":"self_hosted_tts",'
    '"fields":{"model":"kokoro","split_on":"punctuation"}},'
    '"self-hosted-stt":{"type":"self_hosted_stt",'
    '"fields":{"base_url":"http://self-transcribe:8890/v1",'
    '"control_base_url":"http://self-transcribe:8890","model":"base"}}}'
)


def test_manifest_connection_seed_round_trips():
    data = json.loads(MANIFEST_CONNECTION_CONFIGS)
    store = AudioConnectionStore.from_serializable(data)

    by_id = {c.id: c for c in store.list()}
    assert set(by_id) == {"self-hosted-tts", "self-hosted-stt"}
    assert by_id["self-hosted-tts"].type == AudioConnectionType.SELF_HOSTED_TTS
    assert by_id["self-hosted-stt"].type == AudioConnectionType.SELF_HOSTED_STT

    # The store must serialize back to exactly the declared shape, so the manifest
    # value is a faithful, stable representation of what boots.
    assert store.to_serializable() == data


def test_seed_fields_belong_to_their_types():
    # from_serializable is lenient, so assert the declared fields are the ones
    # each type actually presents (a typo'd field would be a silent no-op).
    from selfai_ui.audio.connections import TYPE_FIELD_NAMES

    data = json.loads(MANIFEST_CONNECTION_CONFIGS)
    store = AudioConnectionStore.from_serializable(data)
    for conn in store.list():
        allowed = set(TYPE_FIELD_NAMES[conn.type])
        assert set(conn.fields).issubset(allowed), (
            f"{conn.id} has fields {set(conn.fields)} not in {allowed}"
        )
