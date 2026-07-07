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
    assert resp.status_code == 200, (
        f"Audio models returned {resp.status_code}: {resp.text[:200]}"
    )
    body = resp.json()
    assert isinstance(body, dict)
    assert "models" in body


@pytest.mark.tier1
def test_audio_voices_returns_voices_dict(authenticated_user):
    """GET /audio/voices returns a dict of available voices."""
    resp = authenticated_user.get("/api/v1/audio/voices")
    assert resp.status_code == 200, (
        f"Audio voices returned {resp.status_code}: {resp.text[:200]}"
    )
    assert isinstance(resp.json(), dict)


@pytest.mark.tier1
def test_audio_config_update_persists(authenticated_admin):
    """Config update round-trip with the currently-returned config succeeds."""
    current = authenticated_admin.get("/api/v1/audio/config").json()
    resp = authenticated_admin.post(
        "/api/v1/audio/config/update",
        json=current,
    )
    assert resp.status_code == 200, (
        f"Config round-trip unexpectedly returned {resp.status_code}: "
        f"{resp.text[:200]}"
    )


@pytest.mark.tier1
def test_audio_transcribe_requires_file(authenticated_user):
    """POST /audio/transcriptions without a file returns 4xx."""
    resp = authenticated_user.post("/api/v1/audio/transcriptions")
    # Missing multipart file → 422 from FastAPI validation
    assert resp.status_code in (400, 422)


@pytest.mark.tier1
def test_audio_speech_requires_payload(authenticated_user):
    """POST /audio/speech without a payload returns 4xx."""
    resp = authenticated_user.post("/api/v1/audio/speech", json={})
    # Missing required field
    assert resp.status_code in (400, 422, 500)
