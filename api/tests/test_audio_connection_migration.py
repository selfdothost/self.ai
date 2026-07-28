"""cavekit-audio-connections.md R5 (No Behavior Change on Relocation) — migration.

Verifies each R5 acceptance criterion individually against
``selfai_ui.audio.migration.migrate_legacy_audio_config``:

  AC1: a connection configured under the prior settings-tab arrangement resolves
       to the same backend address after relocation.
  AC2: an existing credential remains valid (byte-for-byte) after relocation.
  AC3: no re-entry / re-authentication is required — the migration is automatic
       and touches nothing the operator must re-enter.
  AC4: a request that succeeded before relocation succeeds after — proven here as
       "the migrated connection's fields exactly match what the old config held"
       (actual STT/TTS request execution is not testable on this bastion).

Plus the idempotency / no-clobber guard that makes the migration safe to run on
every startup.
"""

import itertools

import pytest

from selfai_ui.audio.connections import (
    TYPE_FIELDS,
    AudioConnectionType,
)
from selfai_ui.audio.migration import (
    build_legacy_snapshot,
    migrate_legacy_audio_config,
)


def _counting_id_factory():
    """Deterministic, monotonically-increasing ids for reproducible tests."""

    counter = itertools.count(1)
    return lambda: f"conn-{next(counter)}"


def _migrate(legacy, existing=None):
    return migrate_legacy_audio_config(
        legacy, existing_configs=existing, id_factory=_counting_id_factory()
    )


# A realistic OpenAI-STT + ElevenLabs-TTS prior arrangement.
_LEGACY_OPENAI_STT = {
    "AUDIO_STT_ENGINE": "openai",
    "AUDIO_STT_OPENAI_API_BASE_URL": "https://stt.example.internal/v1",
    "AUDIO_STT_OPENAI_API_KEY": "sk-stt-secret-123",
    "AUDIO_STT_MODEL": "whisper-1",
    "AUDIO_STT_CONTROL_BASE_URL": "",
}
_LEGACY_ELEVENLABS_TTS = {
    "AUDIO_TTS_ENGINE": "elevenlabs",
    "AUDIO_TTS_API_KEY": "el-tts-secret-789",
    "AUDIO_TTS_MODEL": "eleven_multilingual_v2",
    "AUDIO_TTS_VOICE": "Rachel",
    "AUDIO_TTS_SPLIT_ON": "punctuation",
}


# --- AC1: prior-configured connection resolves to the same backend address -----


@pytest.mark.tier0
def test_openai_stt_resolves_to_same_base_url_after_relocation():
    store = _migrate({**_LEGACY_OPENAI_STT, "AUDIO_TTS_ENGINE": ""})

    hosted = store.list_by_type(AudioConnectionType.HOSTED_GENERAL)
    assert len(hosted) == 1
    # The old flat AUDIO_STT_OPENAI_API_BASE_URL is the address; it must survive
    # verbatim onto the typed connection's stt_base_url field.
    assert hosted[0].fields["stt_base_url"] == "https://stt.example.internal/v1"


@pytest.mark.tier0
def test_azure_region_address_survives_relocation():
    legacy = {
        "AUDIO_STT_ENGINE": "",
        "AUDIO_TTS_ENGINE": "azure",
        "AUDIO_TTS_API_KEY": "az-key",
        "AUDIO_TTS_AZURE_SPEECH_REGION": "westeurope",
        "AUDIO_TTS_VOICE": "en-GB-RyanNeural",
        "AUDIO_TTS_AZURE_SPEECH_OUTPUT_FORMAT": "riff-24khz-16bit-mono-pcm",
        "AUDIO_TTS_SPLIT_ON": "punctuation",
    }
    store = _migrate(legacy)

    conns = store.list_by_type(AudioConnectionType.HOSTED_SPECIALIZED_B)
    assert len(conns) == 1
    # Azure's "address" is its region-scoped endpoint selector.
    assert conns[0].fields["region"] == "westeurope"
    assert conns[0].fields["output_format"] == "riff-24khz-16bit-mono-pcm"


# --- AC2: existing credential remains valid (byte-for-byte) after relocation ---


@pytest.mark.tier0
def test_stt_credential_preserved_verbatim():
    store = _migrate({**_LEGACY_OPENAI_STT, "AUDIO_TTS_ENGINE": ""})
    hosted = store.list_by_type(AudioConnectionType.HOSTED_GENERAL)[0]
    assert hosted.fields["stt_api_key"] == "sk-stt-secret-123"


@pytest.mark.tier0
def test_tts_credential_preserved_verbatim():
    store = _migrate({"AUDIO_STT_ENGINE": "", **_LEGACY_ELEVENLABS_TTS})
    spec_a = store.list_by_type(AudioConnectionType.HOSTED_SPECIALIZED_A)[0]
    assert spec_a.fields["api_key"] == "el-tts-secret-789"


@pytest.mark.tier0
def test_credential_is_not_transformed_or_masked():
    # A credential with characters a naive transform might mangle survives exactly.
    tricky = "sk-Ab12/+=key.with:special"
    legacy = {
        "AUDIO_STT_ENGINE": "openai",
        "AUDIO_STT_OPENAI_API_BASE_URL": "https://x/v1",
        "AUDIO_STT_OPENAI_API_KEY": tricky,
        "AUDIO_STT_MODEL": "m",
        "AUDIO_TTS_ENGINE": "",
    }
    store = _migrate(legacy)
    hosted = store.list_by_type(AudioConnectionType.HOSTED_GENERAL)[0]
    assert hosted.fields["stt_api_key"] == tricky


# --- AC3: no re-entry / re-authentication required -----------------------------


@pytest.mark.tier0
def test_migration_is_automatic_no_operator_input():
    # The migration takes only the legacy snapshot — no operator-supplied argument
    # exists, so nothing can be re-entered by construction.
    store = _migrate({**_LEGACY_OPENAI_STT, **_LEGACY_ELEVENLABS_TTS})
    # Two engines were configured -> two independently-addressable connections
    # appear with no further action.
    assert len(store) == 2


@pytest.mark.tier0
def test_transformers_tts_relocates_without_credentials():
    # Self-hosted TTS never had a credential; relocation must not invent an
    # auth step for it (no api_key field on the type at all).
    legacy = {
        "AUDIO_STT_ENGINE": "",
        "AUDIO_TTS_ENGINE": "transformers",
        "AUDIO_TTS_MODEL": "microsoft/speecht5_tts",
        "AUDIO_TTS_SPLIT_ON": "punctuation",
    }
    store = _migrate(legacy)
    conn = store.list_by_type(AudioConnectionType.SELF_HOSTED_TTS)[0]
    assert conn.fields["model"] == "microsoft/speecht5_tts"
    assert "api_key" not in conn.fields


# --- AC4: before/after request-success equivalence -----------------------------
# Proven as: every migrated field exactly equals its legacy source knob, for every
# field the type presents. If the fields match, the same request that succeeded on
# the old flat config succeeds on the relocated connection.


@pytest.mark.tier0
def test_every_hosted_general_field_matches_its_legacy_knob():
    legacy = {
        "AUDIO_STT_ENGINE": "openai",
        "AUDIO_TTS_ENGINE": "openai",
        "AUDIO_STT_OPENAI_API_BASE_URL": "https://stt/v1",
        "AUDIO_STT_OPENAI_API_KEY": "sk-stt",
        "AUDIO_STT_MODEL": "whisper-1",
        "AUDIO_TTS_OPENAI_API_BASE_URL": "https://tts/v1",
        "AUDIO_TTS_OPENAI_API_KEY": "sk-tts",
        "AUDIO_TTS_MODEL": "tts-1-hd",
        "AUDIO_TTS_VOICE": "nova",
        "AUDIO_TTS_SPLIT_ON": "punctuation",
    }
    store = _migrate(legacy)

    # Both selectors point at openai -> ONE HOSTED_GENERAL carrying both sides,
    # not two duplicates.
    hosted = store.list_by_type(AudioConnectionType.HOSTED_GENERAL)
    assert len(hosted) == 1
    conn = hosted[0]

    # Field-for-field: every field equals the exact legacy knob it relocates.
    for connection_field in TYPE_FIELDS[AudioConnectionType.HOSTED_GENERAL]:
        assert conn.fields[connection_field.name] == legacy[connection_field.legacy_config]


@pytest.mark.tier0
@pytest.mark.parametrize(
    "engine, expected_type",
    [
        ("openai", AudioConnectionType.HOSTED_GENERAL),
        ("elevenlabs", AudioConnectionType.HOSTED_SPECIALIZED_A),
        ("azure", AudioConnectionType.HOSTED_SPECIALIZED_B),
        ("transformers", AudioConnectionType.SELF_HOSTED_TTS),
    ],
)
def test_each_tts_engine_relocates_to_its_declared_type(engine, expected_type):
    # Fill every knob any TTS type reads so the resolved type finds its fields.
    legacy = {
        "AUDIO_STT_ENGINE": "",
        "AUDIO_TTS_ENGINE": engine,
        "AUDIO_TTS_OPENAI_API_BASE_URL": "https://tts/v1",
        "AUDIO_TTS_OPENAI_API_KEY": "sk-tts",
        "AUDIO_TTS_API_KEY": "provider-key",
        "AUDIO_TTS_MODEL": "model-x",
        "AUDIO_TTS_VOICE": "voice-y",
        "AUDIO_TTS_AZURE_SPEECH_REGION": "eastus",
        "AUDIO_TTS_AZURE_SPEECH_OUTPUT_FORMAT": "fmt",
        "AUDIO_TTS_SPLIT_ON": "punctuation",
    }
    store = _migrate(legacy)
    conns = store.list_by_type(expected_type)
    assert len(conns) == 1
    # Every field this legacy snapshot actually configured is populated
    # verbatim. A type field with no matching key in the snapshot (e.g.
    # HOSTED_GENERAL's stt_* fields when only TTS was configured) is optional:
    # migration.py leaves it unset rather than fabricating a value (R5).
    for connection_field in TYPE_FIELDS[expected_type]:
        if connection_field.legacy_config not in legacy:
            assert connection_field.name not in conns[0].fields
            continue
        assert conns[0].fields[connection_field.name] == legacy[connection_field.legacy_config]


# --- STT engine mapping (local whisper vs hosted) ------------------------------


@pytest.mark.tier0
def test_local_whisper_stt_relocates_to_self_hosted_when_configured():
    legacy = {
        "AUDIO_STT_ENGINE": "",  # local self-hosted whisper
        "AUDIO_STT_MODEL": "base.en",
        "AUDIO_STT_CONTROL_BASE_URL": "http://transcribe.internal:8093",
        "AUDIO_STT_OPENAI_API_BASE_URL": "http://transcribe.internal/v1",
        "AUDIO_TTS_ENGINE": "",
    }
    store = _migrate(legacy)
    conns = store.list_by_type(AudioConnectionType.SELF_HOSTED_STT)
    assert len(conns) == 1
    assert conns[0].fields["model"] == "base.en"
    assert conns[0].fields["control_base_url"] == "http://transcribe.internal:8093"
    assert conns[0].fields["base_url"] == "http://transcribe.internal/v1"


# --- Idempotency / no-clobber (safe to run on every startup) --------------------


@pytest.mark.tier0
def test_fresh_install_migrates_nothing():
    # Both engines at their empty default and no STT model/control set -> nothing
    # was ever configured, so no phantom connection is minted.
    store = _migrate({"AUDIO_STT_ENGINE": "", "AUDIO_TTS_ENGINE": ""})
    assert len(store) == 0


@pytest.mark.tier0
def test_migration_skipped_when_connections_already_exist():
    # A store the user already populated post-relocation must never be clobbered.
    existing = {
        "user-made-1": {
            "type": AudioConnectionType.HOSTED_SPECIALIZED_A.value,
            "fields": {"api_key": "keep-me", "model": "m", "voice": "v", "split_on": "punctuation"},
        }
    }
    store = _migrate({**_LEGACY_OPENAI_STT, **_LEGACY_ELEVENLABS_TTS}, existing=existing)
    # Only the pre-existing connection remains; no legacy connection was injected.
    assert len(store) == 1
    assert store.get("user-made-1").fields["api_key"] == "keep-me"


@pytest.mark.tier0
def test_migration_is_repeatable_on_its_own_output():
    # First run migrates; feeding its serialized output back in migrates nothing
    # further (the guard is "store already non-empty").
    first = _migrate({**_LEGACY_OPENAI_STT, **_LEGACY_ELEVENLABS_TTS})
    assert len(first) == 2
    second = _migrate(
        {**_LEGACY_OPENAI_STT, **_LEGACY_ELEVENLABS_TTS},
        existing=first.to_serializable(),
    )
    assert second.to_serializable() == first.to_serializable()


# --- build_legacy_snapshot adapter --------------------------------------------


class _FakeKnob:
    def __init__(self, value):
        self.value = value


class _FakeConfigModule:
    """Stand-in for the config module: attributes are PersistentConfig-like knobs."""

    AUDIO_STT_ENGINE = _FakeKnob("openai")
    AUDIO_TTS_ENGINE = _FakeKnob("elevenlabs")
    AUDIO_STT_OPENAI_API_BASE_URL = _FakeKnob("https://stt/v1")
    AUDIO_STT_OPENAI_API_KEY = _FakeKnob("sk-stt")
    AUDIO_STT_MODEL = _FakeKnob("whisper-1")
    AUDIO_STT_CONTROL_BASE_URL = _FakeKnob("")
    AUDIO_TTS_API_KEY = _FakeKnob("el-key")
    AUDIO_TTS_MODEL = _FakeKnob("eleven")
    AUDIO_TTS_VOICE = _FakeKnob("Rachel")
    AUDIO_TTS_SPLIT_ON = _FakeKnob("punctuation")
    AUDIO_TTS_OPENAI_API_BASE_URL = _FakeKnob("https://tts/v1")
    AUDIO_TTS_OPENAI_API_KEY = _FakeKnob("sk-tts")
    AUDIO_TTS_AZURE_SPEECH_REGION = _FakeKnob("eastus")
    AUDIO_TTS_AZURE_SPEECH_OUTPUT_FORMAT = _FakeKnob("fmt")


@pytest.mark.tier0
def test_build_legacy_snapshot_reads_persistentconfig_values():
    snapshot = build_legacy_snapshot(_FakeConfigModule())
    # Engine selectors and field knobs are lifted off ``.value``.
    assert snapshot["AUDIO_STT_ENGINE"] == "openai"
    assert snapshot["AUDIO_STT_OPENAI_API_KEY"] == "sk-stt"
    assert snapshot["AUDIO_TTS_API_KEY"] == "el-key"


@pytest.mark.tier0
def test_snapshot_feeds_migration_end_to_end():
    snapshot = build_legacy_snapshot(_FakeConfigModule())
    store = migrate_legacy_audio_config(snapshot, id_factory=_counting_id_factory())
    # openai STT -> HOSTED_GENERAL; elevenlabs TTS -> HOSTED_SPECIALIZED_A.
    assert len(store.list_by_type(AudioConnectionType.HOSTED_GENERAL)) == 1
    assert len(store.list_by_type(AudioConnectionType.HOSTED_SPECIALIZED_A)) == 1
    assert store.list_by_type(AudioConnectionType.HOSTED_GENERAL)[0].fields["stt_api_key"] == "sk-stt"
