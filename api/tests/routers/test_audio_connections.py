"""Audio connections CRUD router tests (cavekit-audio-connections R1/R2).

Covers the type-first chooser, create/list/update/delete over the persisted
store, field validation, secret masking, and admin gating.
"""

import pytest


@pytest.fixture
def empty_connection_store(test_app):
    """Start each test from an empty persisted connection set."""
    original = getattr(test_app.state.config, "AUDIO_CONNECTION_CONFIGS", {})
    test_app.state.config.AUDIO_CONNECTION_CONFIGS = {}
    try:
        yield
    finally:
        test_app.state.config.AUDIO_CONNECTION_CONFIGS = original


# --- auth ------------------------------------------------------------------

@pytest.mark.tier0
def test_types_requires_admin(authenticated_user):
    assert authenticated_user.get("/api/v1/audio/connections/types").status_code in (401, 403)


@pytest.mark.tier0
def test_list_unauthenticated_rejected(client):
    assert client.get("/api/v1/audio/connections").status_code in (401, 403)


@pytest.mark.tier0
def test_create_requires_admin(authenticated_user):
    resp = authenticated_user.post(
        "/api/v1/audio/connections", json={"type": "self_hosted_tts", "fields": {}}
    )
    assert resp.status_code in (401, 403)


# --- chooser ---------------------------------------------------------------

@pytest.mark.tier1
def test_list_types_returns_five_with_management_actions(authenticated_admin):
    resp = authenticated_admin.get("/api/v1/audio/connections/types")
    assert resp.status_code == 200, resp.text
    types = {t["type"]: t for t in resp.json()["types"]}
    assert set(types) == {
        "self_hosted_stt",
        "self_hosted_tts",
        "hosted_general",
        "hosted_specialized_a",
        "hosted_specialized_b",
    }
    # Self-hosted types expose a management action; hosted ones do not.
    assert types["self_hosted_tts"]["management_action"] is not None
    assert types["self_hosted_stt"]["management_action"] is not None
    assert types["hosted_general"]["management_action"] is None


@pytest.mark.tier1
def test_types_carry_their_field_descriptors(authenticated_admin):
    """R3: each type ships the fields it (and only it) presents, so the
    type-first chooser renders the right form without hardcoding them."""
    resp = authenticated_admin.get("/api/v1/audio/connections/types")
    assert resp.status_code == 200, resp.text
    types = {t["type"]: t for t in resp.json()["types"]}

    def names(t):
        return [f["name"] for f in types[t]["fields"]]

    # Self-hosted TTS is the narrow one: speaker model + text splitting only.
    assert names("self_hosted_tts") == ["model", "split_on"]
    # Self-hosted STT carries its serving + control addresses and model.
    assert names("self_hosted_stt") == ["base_url", "control_base_url", "model"]
    # A field belonging to another type must be absent, not merely disabled.
    assert "base_url" not in names("self_hosted_tts")

    # Every field carries a human label, and credentials are marked secret so a
    # form knows to mask them.
    for t in types.values():
        assert all(f.get("label") for f in t["fields"])
        # legacy_config is an internal migration anchor and must not leak.
        assert all("legacy_config" not in f for f in t["fields"])

    secrets = {f["name"] for f in types["hosted_general"]["fields"] if f["secret"]}
    assert secrets == {"stt_api_key", "tts_api_key"}
    assert not any(f["secret"] for f in types["self_hosted_tts"]["fields"])


# --- create / list ---------------------------------------------------------

@pytest.mark.tier1
def test_create_self_hosted_tts_and_list(authenticated_admin, empty_connection_store):
    resp = authenticated_admin.post(
        "/api/v1/audio/connections",
        json={"type": "self_hosted_tts", "fields": {"model": "glados"}},
    )
    assert resp.status_code == 200, resp.text
    created = resp.json()
    assert created["type"] == "self_hosted_tts"
    assert created["fields"]["model"] == "glados"
    assert created["label"] == "Self-hosted TTS (glados)"
    assert created["management_action"] is not None
    conn_id = created["id"]

    listing = authenticated_admin.get("/api/v1/audio/connections").json()["connections"]
    assert [c["id"] for c in listing] == [conn_id]


@pytest.mark.tier1
def test_create_unknown_type_rejected(authenticated_admin, empty_connection_store):
    resp = authenticated_admin.post(
        "/api/v1/audio/connections", json={"type": "not_a_type", "fields": {}}
    )
    assert resp.status_code == 400


@pytest.mark.tier1
def test_create_unknown_field_rejected(authenticated_admin, empty_connection_store):
    resp = authenticated_admin.post(
        "/api/v1/audio/connections",
        json={"type": "self_hosted_tts", "fields": {"base_url": "http://x"}},
    )
    # base_url is not a field of self_hosted_tts (model / split_on only).
    assert resp.status_code == 400


@pytest.mark.tier1
def test_two_same_type_connections_coexist(authenticated_admin, empty_connection_store):
    a = authenticated_admin.post(
        "/api/v1/audio/connections",
        json={"type": "self_hosted_tts", "fields": {"model": "a"}},
    ).json()
    b = authenticated_admin.post(
        "/api/v1/audio/connections",
        json={"type": "self_hosted_tts", "fields": {"model": "b"}},
    ).json()
    assert a["id"] != b["id"]
    ids = [c["id"] for c in authenticated_admin.get("/api/v1/audio/connections").json()["connections"]]
    assert set(ids) == {a["id"], b["id"]}


# --- update / delete -------------------------------------------------------

@pytest.mark.tier1
def test_update_merges_fields(authenticated_admin, empty_connection_store):
    conn = authenticated_admin.post(
        "/api/v1/audio/connections",
        json={"type": "self_hosted_stt", "fields": {"model": "base"}},
    ).json()
    resp = authenticated_admin.patch(
        f"/api/v1/audio/connections/{conn['id']}",
        json={"fields": {"control_base_url": "http://self-transcribe:8890"}},
    )
    assert resp.status_code == 200, resp.text
    fields = resp.json()["fields"]
    assert fields["model"] == "base"
    assert fields["control_base_url"] == "http://self-transcribe:8890"


@pytest.mark.tier1
def test_update_unknown_id_404(authenticated_admin, empty_connection_store):
    resp = authenticated_admin.patch(
        "/api/v1/audio/connections/nope", json={"fields": {"model": "x"}}
    )
    assert resp.status_code == 404


@pytest.mark.tier1
def test_delete_removes_only_that_connection(authenticated_admin, empty_connection_store):
    a = authenticated_admin.post(
        "/api/v1/audio/connections",
        json={"type": "self_hosted_tts", "fields": {"model": "a"}},
    ).json()
    b = authenticated_admin.post(
        "/api/v1/audio/connections",
        json={"type": "self_hosted_tts", "fields": {"model": "b"}},
    ).json()

    assert authenticated_admin.delete(f"/api/v1/audio/connections/{a['id']}").status_code == 200
    ids = [c["id"] for c in authenticated_admin.get("/api/v1/audio/connections").json()["connections"]]
    assert ids == [b["id"]]

    assert authenticated_admin.delete("/api/v1/audio/connections/nope").status_code == 404


# --- secret masking --------------------------------------------------------

@pytest.mark.tier1
def test_secret_fields_masked_in_responses(authenticated_admin, empty_connection_store):
    resp = authenticated_admin.post(
        "/api/v1/audio/connections",
        json={
            "type": "hosted_general",
            "fields": {"stt_api_key": "sk-super-secret", "stt_model": "whisper-1"},
        },
    )
    assert resp.status_code == 200, resp.text
    fields = resp.json()["fields"]
    # The secret is masked; a non-secret field is echoed verbatim.
    assert fields["stt_api_key"] != "sk-super-secret"
    assert fields["stt_model"] == "whisper-1"

    listed = authenticated_admin.get("/api/v1/audio/connections").json()["connections"][0]
    assert listed["fields"]["stt_api_key"] != "sk-super-secret"


# --- persistence -----------------------------------------------------------

@pytest.mark.tier1
def test_create_persists_to_config(authenticated_admin, test_app, empty_connection_store):
    conn = authenticated_admin.post(
        "/api/v1/audio/connections",
        json={"type": "self_hosted_tts", "fields": {"model": "glados"}},
    ).json()
    persisted = test_app.state.config.AUDIO_CONNECTION_CONFIGS
    assert conn["id"] in persisted
    assert persisted[conn["id"]]["type"] == "self_hosted_tts"
    # The persisted store keeps the real value (masking is a response concern).
    assert persisted[conn["id"]]["fields"]["model"] == "glados"
