"""Audio router tests — STT/TTS config and model/voice listing."""

import pytest


@pytest.mark.tier0
def test_audio_config_admin_only(authenticated_user):
    resp = authenticated_user.get("/api/v1/audio/config")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_audio_config_admin_access(authenticated_admin):
    resp = authenticated_admin.get("/api/v1/audio/config")
    assert resp.status_code == 200


@pytest.mark.tier0
def test_audio_models_user_access(authenticated_user):
    """TTS models listing requires auth but not admin."""
    resp = authenticated_user.get("/api/v1/audio/models")
    assert resp.status_code == 200


@pytest.mark.tier0
def test_audio_models_unauthenticated_rejected(client):
    resp = client.get("/api/v1/audio/models")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_audio_voices_user_access(authenticated_user):
    resp = authenticated_user.get("/api/v1/audio/voices")
    assert resp.status_code == 200
