"""cavekit-audio-voice-picker.md R5 (No Wire-Contract Regression) — T-019.

R5 requires that the *programmatic* TTS-serving path is unchanged by the additive
UI work this build round added (the Tier 2-6 voice catalog / curation /
aggregation / picker features). Specifically:

  AC1: a programmatic request that specifies a voice by identifier is honored
       exactly as before this kit ships.
  AC2: the identifier accepted programmatically is unchanged — no rename or
       re-shaping of the voice identifier on the wire.
  AC3: the programmatic path does not require any of the new UI metadata (name,
       language, gender, enabled-flag, curation state) to succeed.

This is a **verify-and-document** task, not a gap-fix — mirroring the T-007
precedent (``test_audio_serving_engine_preserved.py``). The real TTS-serving
path is the pre-existing ``selfai_ui/routers/audio.py`` ``POST /speech`` handler
(defined at line 239). It reads the voice identifier straight from the incoming
request body under the field name ``voice`` (``payload.get("voice", "")`` at line
315 for ElevenLabs; the whole ``payload`` — including its ``voice`` field — is
forwarded verbatim to the upstream for OpenAI at line 270 ``json=payload``) and
forwards it to the backend provider without consulting any of the new catalog
work.

The new picker/curation/aggregation code lives entirely in separate modules
(``selfai_ui/audio/voice_catalog.py``, ``selfai_ui/routers/voice_catalog.py``)
and the serving router imports and references *none* of it (verified below and
by git history: no picker-round commit touched ``routers/audio.py``). The one
validation the ElevenLabs branch performs — ``voice_id not in
get_available_voices(request)`` at line 317 — checks the *raw provider* voice
list (``get_elevenlabs_voices`` fetching ``/v1/voices`` from ElevenLabs), NOT the
new curated/enabled catalog, so no new curation requirement leaked onto the
serving path.

These tests prove each R5 criterion individually against the current code, with
both source-invariant assertions (following the T-007 methodology) and concrete
behavioral tests that drive the ``speech()`` handler and capture what reaches the
upstream provider.
"""

import json
import pathlib

import pytest

import selfai_ui.routers.audio as serving

# Path to the pre-existing serving router, resolved off this test file's location
# so the source-invariant tests do not depend on import side effects.
_SERVING_ROUTER = (
    pathlib.Path(__file__).resolve().parents[1]
    / "selfai_ui"
    / "routers"
    / "audio.py"
)


# --------------------------------------------------------------------------- #
# Test doubles: a minimal Request and a fake aiohttp session that captures     #
# exactly what the serving handler posts to the upstream provider.             #
# --------------------------------------------------------------------------- #


class _Cfg:
    """Only the flat serving selectors + per-engine knobs the handler reads."""

    def __init__(self, **kw):
        # sensible defaults; individual tests override the engine + knobs
        self.TTS_ENGINE = "openai"
        self.TTS_MODEL = "tts-1"
        self.TTS_OPENAI_API_BASE_URL = "https://tts.internal/v1"
        self.TTS_OPENAI_API_KEY = "sk-test"
        self.TTS_API_KEY = "el-test"
        self.__dict__.update(kw)


class _State:
    def __init__(self, cfg):
        self.config = cfg


class _App:
    def __init__(self, cfg):
        self.state = _State(cfg)


class _Req:
    def __init__(self, body: bytes, cfg: _Cfg):
        self._body = body
        self.app = _App(cfg)

    async def body(self) -> bytes:
        return self._body


class _User:
    id = "u1"
    name = "Tester"
    email = "t@example.com"
    role = "user"


class _FakeResp:
    status = 200

    def __init__(self, capture):
        self._capture = capture

    def raise_for_status(self):
        return None

    async def read(self):
        return b"FAKE-AUDIO-BYTES"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Captures the URL / json / headers of the single upstream POST."""

    def __init__(self, capture):
        self._capture = capture

    def post(self, url, json=None, headers=None, data=None, **kw):
        self._capture["url"] = url
        self._capture["json"] = json
        self._capture["headers"] = headers
        self._capture["data"] = data
        return _FakeResp(self._capture)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Point the speech cache at a real writable tmp dir so aiofiles + the
    FileResponse return path work without touching the real cache."""
    d = tmp_path / "speech"
    d.mkdir()
    monkeypatch.setattr(serving, "SPEECH_CACHE_DIR", d)
    return d


@pytest.fixture
def capture(monkeypatch):
    cap = {}
    monkeypatch.setattr(
        serving.aiohttp, "ClientSession", lambda *a, **k: _FakeSession(cap)
    )
    return cap


async def _call_speech(cfg: _Cfg, body: dict):
    req = _Req(json.dumps(body).encode("utf-8"), cfg)
    return await serving.speech(req, user=_User())


# --------------------------------------------------------------------------- #
# AC1 — a programmatic voice-by-identifier request is honored as before.       #
# --------------------------------------------------------------------------- #


@pytest.mark.tier0
def test_openai_speech_forwards_body_voice_identifier_verbatim(cache_dir, capture):
    """AC1 + AC2: OpenAI TTS forwards the whole request body — including the
    caller-supplied ``voice`` id — to the upstream unchanged."""
    import asyncio

    cfg = _Cfg(TTS_ENGINE="openai")
    resp = asyncio.run(_call_speech(cfg, {"input": "hello", "voice": "nova"}))

    # the identifier reaches the provider under the same field name, byte-identical
    assert capture["json"]["voice"] == "nova"
    assert capture["json"]["input"] == "hello"
    # the handler only augments the payload with the deploy-time model, nothing else
    assert capture["json"]["model"] == "tts-1"
    assert set(capture["json"].keys()) == {"input", "voice", "model"}
    # and it returns the synthesized file, i.e. the request was honored
    assert resp is not None


@pytest.mark.tier0
def test_elevenlabs_speech_routes_body_voice_identifier_verbatim_into_url(
    cache_dir, capture, monkeypatch
):
    """AC1 + AC2: ElevenLabs TTS places the caller-supplied ``voice`` id straight
    into the provider URL path with no re-shaping."""
    import asyncio

    # The pre-existing validation checks the RAW provider voice list, not the new
    # curated/enabled catalog. Stand that raw list in directly.
    monkeypatch.setattr(
        serving, "get_available_voices", lambda request: {"EXAVITQu4vr4xnSDxMaL": "Bella"}
    )

    cfg = _Cfg(TTS_ENGINE="elevenlabs", TTS_MODEL="eleven_multilingual_v2")
    asyncio.run(
        _call_speech(cfg, {"input": "hi", "voice": "EXAVITQu4vr4xnSDxMaL"})
    )

    # the id is not renamed/reshaped: it appears verbatim in the upstream path
    assert (
        capture["url"]
        == "https://api.elevenlabs.io/v1/text-to-speech/EXAVITQu4vr4xnSDxMaL"
    )


# --------------------------------------------------------------------------- #
# AC2 — the accepted identifier field on the wire is unchanged (no rename).     #
# --------------------------------------------------------------------------- #


@pytest.mark.tier0
def test_speech_reads_the_voice_identifier_from_the_body_field_named_voice():
    """AC2: the request-body field carrying the voice id is still ``voice`` —
    the picker round introduced no rename on the wire."""
    src = _SERVING_ROUTER.read_text()
    # ElevenLabs branch reads the caller's voice from payload["voice"]
    assert 'payload.get("voice", "")' in src
    # no alternate/renamed field name for the programmatic voice id was introduced
    for renamed in ("voice_identifier", "voice_id_field", "catalog_voice", "voiceId"):
        assert renamed not in src, f"serving router introduced renamed field {renamed!r}"


@pytest.mark.tier0
def test_openai_branch_forwards_the_body_payload_verbatim():
    """AC2: the OpenAI branch forwards the caller's body as-is (``json=payload``),
    so the voice field is not re-shaped in transit."""
    src = _SERVING_ROUTER.read_text()
    assert "json=payload," in src


# --------------------------------------------------------------------------- #
# AC3 — the programmatic path requires none of the new UI metadata.            #
# --------------------------------------------------------------------------- #


@pytest.mark.tier0
def test_openai_speech_succeeds_with_only_input_and_voice(cache_dir, capture):
    """AC3: a body carrying ONLY the identifier (plus the text to speak) — no
    name / language / gender / enabled / curation keys — succeeds."""
    import asyncio

    cfg = _Cfg(TTS_ENGINE="openai")
    resp = asyncio.run(_call_speech(cfg, {"input": "hello", "voice": "shimmer"}))

    assert resp is not None
    # nothing from the new catalog metadata was needed or forwarded
    for meta in ("name", "language", "gender", "enabled", "curation", "source"):
        assert meta not in capture["json"]


@pytest.mark.tier0
def test_serving_router_never_consults_the_new_voice_catalog_work():
    """AC3: the serving router is not wired to any Tier 2-6 picker/curation work.

    No new metadata requirement was introduced onto the programmatic path: the
    serving router references neither the catalog builder/aggregator/curator
    functions nor the new catalog router endpoints. Which voice serves is still
    decided purely by the caller-supplied ``voice`` field, exactly as before.
    """
    src = _SERVING_ROUTER.read_text()
    for forbidden in (
        "voice_catalog",
        "build_voice_catalog",
        "aggregate_voice_catalogs",
        "curate_voice_catalog",
        "filter_enabled_voices",
        "voices/curation",
        "voices/selectable",
        "voices/aggregated",
        "voices/enabled",
    ):
        assert forbidden not in src, f"serving router references {forbidden!r}"


@pytest.mark.tier0
def test_serving_router_imports_none_of_the_new_catalog_modules():
    """AC3: the serving module object imports neither new catalog module, so the
    programmatic path cannot have taken on a curated/enabled-catalog dependency."""
    import sys

    src = _SERVING_ROUTER.read_text()
    assert "from selfai_ui.audio.voice_catalog" not in src
    assert "import selfai_ui.audio.voice_catalog" not in src
    assert "routers.voice_catalog" not in src
    # and, defensively, the imported module namespace exposes no catalog symbols
    mod = sys.modules[serving.__name__]
    for sym in (
        "build_voice_catalog",
        "aggregate_voice_catalogs",
        "curate_voice_catalog",
        "filter_enabled_voices",
    ):
        assert not hasattr(mod, sym), f"serving router pulled in {sym!r}"


@pytest.mark.tier0
def test_elevenlabs_validation_is_against_the_raw_provider_list_not_curation(
    cache_dir, capture, monkeypatch
):
    """AC3 guard: the only voice validation on the serving path checks the RAW
    provider voice list, not the new curated/enabled catalog — so a voice absent
    from curation still serves as long as the provider offers it."""
    import asyncio

    # Raw provider offers the voice; it is NOT in any curated/enabled catalog here.
    monkeypatch.setattr(
        serving,
        "get_available_voices",
        lambda request: {"provider-only-voice": "Provider Only"},
    )
    cfg = _Cfg(TTS_ENGINE="elevenlabs", TTS_MODEL="eleven_multilingual_v2")

    # succeeds purely on the raw provider list — no curation lookup required
    asyncio.run(_call_speech(cfg, {"input": "hi", "voice": "provider-only-voice"}))
    assert capture["url"].endswith("/provider-only-voice")
