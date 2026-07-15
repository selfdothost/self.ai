"""
T-R03 (part 4) + T-R11: Audio router forwarding.

Audio router uses aiohttp for speech/transcribe and mixed patterns
for models/voices. Tests use aioresponses + responses as appropriate.

Full transcribe/synthesize forwarding is dependent on a configured
TTS/STT engine; we cover the admin-only config paths and the
models/voices endpoints which depend on config state.
"""

import pytest


@pytest.mark.tier1
def test_audio_models_returns_models_dict(authenticated_user, test_app):
    """GET /audio/models returns {models: [...]} shape."""
    resp = authenticated_user.get("/api/v1/audio/models")
    assert resp.status_code == 200, f"Audio models returned {resp.status_code}: {resp.text[:200]}"
    body = resp.json()
    assert isinstance(body, dict)
    assert "models" in body


@pytest.mark.tier1
def test_audio_voices_returns_voices_dict(authenticated_user):
    """GET /audio/voices returns a dict of available voices."""
    resp = authenticated_user.get("/api/v1/audio/voices")
    assert resp.status_code == 200, f"Audio voices returned {resp.status_code}: {resp.text[:200]}"
    assert isinstance(resp.json(), dict)


@pytest.mark.tier1
def test_audio_config_update_persists(authenticated_admin):
    """Config round-trip with an external STT engine configured succeeds.

    STT_ENGINE="" means "use the local faster-whisper engine" -- not
    supported on the API-tier image (self.ai#26), so the round-trip must
    use an external engine to exercise the actually-supported path. See
    test_audio_config_update_local_whisper_unavailable below for the local
    case, which is expected to 501 here, not 200.
    """
    current = authenticated_admin.get("/api/v1/audio/config").json()
    current["stt"]["ENGINE"] = "openai"
    resp = authenticated_admin.post(
        "/api/v1/audio/config/update",
        json=current,
    )
    assert resp.status_code == 200, f"Config round-trip unexpectedly returned {resp.status_code}: " f"{resp.text[:200]}"


@pytest.mark.tier1
def test_audio_config_update_local_whisper_unavailable(authenticated_admin):
    """STT_ENGINE="" (local whisper) 501s on the API-tier image (self.ai#26)
    instead of crashing with ModuleNotFoundError."""
    current = authenticated_admin.get("/api/v1/audio/config").json()
    current["stt"]["ENGINE"] = ""
    resp = authenticated_admin.post(
        "/api/v1/audio/config/update",
        json=current,
    )
    assert resp.status_code == 501, (
        f"Expected 501 for the unavailable local engine, got {resp.status_code}: " f"{resp.text[:200]}"
    )
    assert "not available on this image" in resp.json()["detail"]


@pytest.mark.tier1
def test_audio_transcribe_requires_file(authenticated_user):
    """POST /audio/transcriptions without a file returns 4xx."""
    resp = authenticated_user.post("/api/v1/audio/transcriptions")
    # Missing multipart file → 422 from FastAPI validation
    assert resp.status_code in (400, 422)


@pytest.mark.tier1
def test_audio_speech_unconfigured_engine_rejected(authenticated_user):
    """POST /audio/speech with no TTS engine configured returns 400, not a
    silent 200/null (self.ai#26) -- speech()'s engine dispatch previously had
    no else clause, so an unmatched TTS_ENGINE fell through with an implicit
    return, which FastAPI turns into a 200 with an empty body."""
    resp = authenticated_user.post("/api/v1/audio/speech", json={})
    assert resp.status_code == 400, (
        f"Expected 400 for an unconfigured TTS engine, got {resp.status_code}: " f"{resp.text[:200]}"
    )
    assert resp.json()["detail"] == "TTS engine is not configured"
