"""cavekit-audio-connections.md R6 (Engine Selection Unaffected) — T-007.

R6 requires that relocating the audio backend fields into the typed Connections
surface does NOT change which connection actually serves a given STT or TTS
request, and that this kit introduces no new per-request serving-engine mechanism
(that concern is owned elsewhere and left unchanged).

This is a **verify-and-document** task, not a gap-fix. The serving path — the
pre-existing request-serving router ``selfai_ui/routers/audio.py`` — was authored
before the audio round and has not been touched by any connections-catalog task
(T-001..T-031). Its STT/TTS dispatch keys solely off the flat serving selectors
``config.STT_ENGINE`` / ``config.TTS_ENGINE`` (set from ``AUDIO_STT_ENGINE`` /
``AUDIO_TTS_ENGINE`` in ``main.py``). The connections catalog and the T-006
migration are additive: the migration *reads* those flat selectors to decide
which typed connection each capability relocates into, but never *writes* them,
and the serving router never consults the connection store. So which connection
serves a request is identical before and after relocation.

These tests prove each R6 criterion individually against the current code:

  AC1: the connection serving a given STT request is unchanged after relocation.
       -> STT serving keys off ``STT_ENGINE`` only (``routers/audio.py``, the
          ``transcribe`` fn, lines 471/497), and migration leaves ``STT_ENGINE``
          byte-identical.
  AC2: the connection serving a given TTS request is unchanged after relocation.
       -> TTS serving keys off ``TTS_ENGINE`` only (``routers/audio.py``, the
          ``/speech`` handler, lines 262/314/365/418), and migration leaves
          ``TTS_ENGINE`` byte-identical.
  AC3: no new per-request serving-engine mechanism is introduced.
       -> the serving router references no part of the connections catalog
          (no store, no migration, no ``AUDIO_CONNECTION_CONFIGS``), and no
          migrated connection carries an engine/serving-override field (the
          ``ENGINE`` selector is the sole omission from the relocated field sets,
          per T-004).

Run convention matches the rest of this domain: pure/source-level assertions run
anywhere; the migration import needs the app package (present in CI).
"""

import itertools
import pathlib

import pytest

from selfai_ui.audio.migration import migrate_legacy_audio_config


def _counting_id_factory():
    counter = itertools.count(1)
    return lambda: f"conn-{next(counter)}"


def _migrate(legacy):
    return migrate_legacy_audio_config(
        legacy, existing_configs={}, id_factory=_counting_id_factory()
    )


# Path to the pre-existing serving router, resolved off this test file's location
# so the source-invariant tests run without importing the FastAPI app.
_SERVING_ROUTER = (
    pathlib.Path(__file__).resolve().parents[1]
    / "selfai_ui"
    / "routers"
    / "audio.py"
)


# A realistic prior arrangement: OpenAI STT + ElevenLabs TTS, plus every sibling
# knob those engines read, plus the two flat serving selectors.
_LEGACY = {
    "AUDIO_STT_ENGINE": "openai",
    "AUDIO_STT_OPENAI_API_BASE_URL": "https://stt.internal/v1",
    "AUDIO_STT_OPENAI_API_KEY": "sk-stt-secret",
    "AUDIO_STT_MODEL": "whisper-1",
    "AUDIO_STT_CONTROL_BASE_URL": "",
    "AUDIO_TTS_ENGINE": "elevenlabs",
    "AUDIO_TTS_API_KEY": "el-secret",
    "AUDIO_TTS_MODEL": "eleven_multilingual_v2",
    "AUDIO_TTS_VOICE": "Rachel",
    "AUDIO_TTS_SPLIT_ON": "punctuation",
}


# --- AC1 / AC2: migration relocates without touching the serving selectors -----


@pytest.mark.tier0
def test_migration_does_not_mutate_the_legacy_snapshot():
    """The migration is read-only over the flat config it consumes.

    It reads ``AUDIO_STT_ENGINE`` / ``AUDIO_TTS_ENGINE`` (and the sibling knobs)
    to decide relocation, but must not write any of them — so the values that
    drive serving in ``routers/audio.py`` are unchanged.
    """
    legacy = dict(_LEGACY)
    before = dict(legacy)

    _migrate(legacy)

    assert legacy == before


@pytest.mark.tier0
def test_relocation_actually_happened_so_the_check_is_not_vacuous():
    """Guards AC1/AC2: prove the migration really relocated connections.

    Without this, "selectors unchanged" could pass trivially on a no-op. Here
    OpenAI-STT + ElevenLabs-TTS relocate into two distinct typed connections.
    """
    store = _migrate(dict(_LEGACY))
    serialized = store.to_serializable()

    assert len(serialized) == 2
    types = {entry["type"] for entry in serialized.values()}
    assert types == {"hosted_general", "hosted_specialized_a"}


@pytest.mark.tier0
def test_stt_serving_selector_is_unchanged_after_relocation():
    """AC1: the STT serving selector value survives relocation byte-for-byte."""
    legacy = dict(_LEGACY)
    _migrate(legacy)
    assert legacy["AUDIO_STT_ENGINE"] == _LEGACY["AUDIO_STT_ENGINE"] == "openai"


@pytest.mark.tier0
def test_tts_serving_selector_is_unchanged_after_relocation():
    """AC2: the TTS serving selector value survives relocation byte-for-byte."""
    legacy = dict(_LEGACY)
    _migrate(legacy)
    assert legacy["AUDIO_TTS_ENGINE"] == _LEGACY["AUDIO_TTS_ENGINE"] == "elevenlabs"


@pytest.mark.tier0
@pytest.mark.parametrize("stt_engine", ["", "openai"])
@pytest.mark.parametrize(
    "tts_engine", ["transformers", "openai", "elevenlabs", "azure"]
)
def test_every_serving_selector_combo_survives_migration_unchanged(
    stt_engine, tts_engine
):
    """AC1 + AC2 across the whole engine matrix.

    For each pairing of a self-hosted/hosted STT engine with each TTS engine,
    the flat selectors that ``routers/audio.py`` dispatches on are identical
    before and after the migration runs.
    """
    legacy = {
        "AUDIO_STT_ENGINE": stt_engine,
        # a self-hosted ("") STT counts as configured only with a model set
        "AUDIO_STT_MODEL": "small" if stt_engine == "" else "",
        "AUDIO_STT_OPENAI_API_BASE_URL": "https://stt.internal/v1",
        "AUDIO_STT_OPENAI_API_KEY": "sk-stt",
        "AUDIO_STT_CONTROL_BASE_URL": "",
        "AUDIO_TTS_ENGINE": tts_engine,
        "AUDIO_TTS_API_KEY": "tts-key",
        "AUDIO_TTS_MODEL": "tts-model",
        "AUDIO_TTS_VOICE": "voice",
        "AUDIO_TTS_SPLIT_ON": "punctuation",
        "AUDIO_TTS_OPENAI_API_BASE_URL": "https://tts.internal/v1",
        "AUDIO_TTS_OPENAI_API_KEY": "sk-tts",
    }
    before = dict(legacy)

    _migrate(legacy)

    assert legacy["AUDIO_STT_ENGINE"] == before["AUDIO_STT_ENGINE"] == stt_engine
    assert legacy["AUDIO_TTS_ENGINE"] == before["AUDIO_TTS_ENGINE"] == tts_engine


# --- AC3: no new per-request serving-engine mechanism ---------------------------


@pytest.mark.tier0
def test_no_migrated_connection_carries_an_engine_override_field():
    """AC3: the relocated connection model introduces no new serving selector.

    T-004 relocated every per-engine field EXCEPT the old ``ENGINE`` selector —
    because the connection *type* now is the engine. So no minted connection may
    carry an ``engine``/``ENGINE`` field that could act as a per-request serving
    override.
    """
    store = _migrate(dict(_LEGACY))
    for entry in store.to_serializable().values():
        assert "engine" not in entry["fields"]
        assert "ENGINE" not in entry["fields"]


@pytest.mark.tier0
def test_tts_serving_dispatches_only_on_the_flat_tts_engine():
    """AC2 underpinning: the /speech handler branches on ``TTS_ENGINE``."""
    src = _SERVING_ROUTER.read_text()
    for engine in ("openai", "elevenlabs", "azure", "transformers"):
        assert f'request.app.state.config.TTS_ENGINE == "{engine}"' in src


@pytest.mark.tier0
def test_stt_serving_dispatches_only_on_the_flat_stt_engine():
    """AC1 underpinning: the transcribe fn branches on ``STT_ENGINE``."""
    src = _SERVING_ROUTER.read_text()
    assert 'request.app.state.config.STT_ENGINE == ""' in src
    assert 'request.app.state.config.STT_ENGINE == "openai"' in src


@pytest.mark.tier0
def test_serving_router_never_consults_the_connections_catalog():
    """AC3: the pre-existing serving router is not wired to the new catalog.

    No new per-request serving mechanism was introduced: the serving router
    references neither the typed-connection store, the migration, nor the
    ``AUDIO_CONNECTION_CONFIGS`` persisted set. Which connection serves is still
    decided purely by the flat ``*_ENGINE`` selector, exactly as before.
    """
    src = _SERVING_ROUTER.read_text()
    for forbidden in (
        "AUDIO_CONNECTION_CONFIGS",
        "AudioConnectionStore",
        "SavedAudioConnection",
        "audio.connections",
        "audio.migration",
        "connection_store",
    ):
        assert forbidden not in src, f"serving router references {forbidden!r}"
