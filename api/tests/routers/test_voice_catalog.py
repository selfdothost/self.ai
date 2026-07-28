"""Voice-catalog router endpoint tests (cavekit-audio-voice-catalog R1).

The catalog endpoint fetches the self-hosted TTS backend's real voice list over
aiohttp; upstream is mocked with aioresponses. Covers auth gating, the
unconfigured-backend guard, and the "real voices only, no placeholder" contract
including the two-different-backends -> two-different-catalogs criterion.
"""

import pytest

from tests.mocks.external_services import aioresponses_strict

CONTROL_URL = "http://self-speak:9100"
CONTROL_URL_B = "http://self-speak-b:9100"


@pytest.fixture
def tts_control_configured(test_app):
    """Point the router at a self-hosted TTS control backend for the test."""
    original = getattr(test_app.state.config, "TTS_CONTROL_BASE_URL", "")
    test_app.state.config.TTS_CONTROL_BASE_URL = CONTROL_URL
    try:
        yield
    finally:
        test_app.state.config.TTS_CONTROL_BASE_URL = original


@pytest.mark.tier0
def test_voice_catalog_requires_admin(authenticated_user, tts_control_configured):
    """The real catalog is an admin/management surface — a plain user is rejected."""
    resp = authenticated_user.get("/api/v1/voice-catalog/voices")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_voice_catalog_unauthenticated_rejected(client, tts_control_configured):
    resp = client.get("/api/v1/voice-catalog/voices")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_voice_catalog_unconfigured_backend(authenticated_admin, test_app):
    """No control URL configured -> 400 (client/config condition)."""
    original = getattr(test_app.state.config, "TTS_CONTROL_BASE_URL", "")
    test_app.state.config.TTS_CONTROL_BASE_URL = ""
    try:
        resp = authenticated_admin.get("/api/v1/voice-catalog/voices")
        assert resp.status_code == 400
    finally:
        test_app.state.config.TTS_CONTROL_BASE_URL = original


@pytest.mark.tier1
def test_voice_catalog_lists_real_backend_voices(authenticated_admin, tts_control_configured):
    """AC1: the catalog matches the voices the connection's backend reports."""
    with aioresponses_strict() as m:
        m.get(
            f"{CONTROL_URL}/api/voices",
            status=200,
            payload={"voices": [{"id": "en-us-amy"}, {"id": "en-us-ryan"}]},
        )
        resp = authenticated_admin.get("/api/v1/voice-catalog/voices")

    assert resp.status_code == 200, resp.text
    ids = [v["id"] for v in resp.json()["voices"]]
    assert ids == ["en-us-amy", "en-us-ryan"]


@pytest.mark.tier1
def test_two_connections_return_different_catalogs(authenticated_admin, test_app):
    """AC2: repointing at a different backend returns that backend's own voices.

    Same endpoint, two different self-hosted TTS connection addresses -> two
    different voice lists, proving there is no shared hardcoded placeholder.
    """
    original = getattr(test_app.state.config, "TTS_CONTROL_BASE_URL", "")
    try:
        test_app.state.config.TTS_CONTROL_BASE_URL = CONTROL_URL
        with aioresponses_strict() as m:
            m.get(
                f"{CONTROL_URL}/api/voices",
                status=200,
                payload={"voices": [{"id": "alpha-1"}, {"id": "alpha-2"}]},
            )
            resp_a = authenticated_admin.get("/api/v1/voice-catalog/voices")

        test_app.state.config.TTS_CONTROL_BASE_URL = CONTROL_URL_B
        with aioresponses_strict() as m:
            m.get(
                f"{CONTROL_URL_B}/api/voices",
                status=200,
                payload={"voices": [{"id": "beta-1"}]},
            )
            resp_b = authenticated_admin.get("/api/v1/voice-catalog/voices")
    finally:
        test_app.state.config.TTS_CONTROL_BASE_URL = original

    ids_a = [v["id"] for v in resp_a.json()["voices"]]
    ids_b = [v["id"] for v in resp_b.json()["voices"]]
    assert ids_a == ["alpha-1", "alpha-2"]
    assert ids_b == ["beta-1"]
    assert set(ids_a).isdisjoint(ids_b)


@pytest.mark.tier1
def test_absent_voice_not_reported(authenticated_admin, tts_control_configured):
    """AC3: a voice the backend does not report never appears in the catalog."""
    with aioresponses_strict() as m:
        m.get(
            f"{CONTROL_URL}/api/voices",
            status=200,
            payload={"voices": [{"id": "present-voice"}]},
        )
        resp = authenticated_admin.get("/api/v1/voice-catalog/voices")

    assert resp.status_code == 200, resp.text
    ids = [v["id"] for v in resp.json()["voices"]]
    assert ids == ["present-voice"]
    assert "absent-voice" not in ids


@pytest.mark.tier1
def test_voice_catalog_empty_backend(authenticated_admin, tts_control_configured):
    """A backend offering no voices -> empty catalog, 200 (not a default set)."""
    with aioresponses_strict() as m:
        m.get(f"{CONTROL_URL}/api/voices", status=200, payload={"voices": []})
        resp = authenticated_admin.get("/api/v1/voice-catalog/voices")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"voices": []}


@pytest.mark.tier1
def test_voice_catalog_carries_voice_metadata(authenticated_admin, tts_control_configured):
    """R2 AC1-AC4: each returned voice carries name/language/gender.

    A fully-described voice exposes all three; an id-only voice still carries a
    name (its id) and null language/gender — never a bare identifier.
    """
    with aioresponses_strict() as m:
        m.get(
            f"{CONTROL_URL}/api/voices",
            status=200,
            payload={
                "voices": [
                    {"id": "en-us-amy", "name": "Amy", "language": "en-US", "gender": "female"},
                    {"id": "bare-voice"},
                ]
            },
        )
        resp = authenticated_admin.get("/api/v1/voice-catalog/voices")

    assert resp.status_code == 200, resp.text
    voices = {v["id"]: v for v in resp.json()["voices"]}

    amy = voices["en-us-amy"]
    assert amy["name"] == "Amy"
    assert amy["language"] == "en-US"
    assert amy["gender"] == "female"

    bare = voices["bare-voice"]
    assert bare["name"] == "bare-voice"  # AC4: falls back to id, never absent
    assert bare["language"] is None
    assert bare["gender"] is None
    assert set(bare.keys()) == {"id", "name", "language", "gender"}


@pytest.mark.tier1
def test_voice_catalog_backend_unreachable(authenticated_admin, tts_control_configured):
    """Backend unreachable -> 502 (upstream fault, not a silent placeholder)."""
    with aioresponses_strict() as m:
        m.get(f"{CONTROL_URL}/api/voices", status=503, payload={"error": "down"})
        resp = authenticated_admin.get("/api/v1/voice-catalog/voices")

    assert resp.status_code == 502


# ---------------------------------------------------------------------------
# R5 — enabled-only end-user catalog + admin toggle (T-014)
#   AC1 — the end-user catalog omits any voice an admin has disabled.
#   AC2 — a disabled voice remains present in the admin-facing catalog.
#   AC3 — enabling/disabling a voice adds/removes it from the end-user catalog.
# ---------------------------------------------------------------------------


@pytest.fixture
def tts_enabled_voices(test_app):
    """Isolate the admin-set enabled-voices curation map per test."""
    original = getattr(test_app.state.config, "TTS_ENABLED_VOICES", {})
    test_app.state.config.TTS_ENABLED_VOICES = {}
    try:
        yield
    finally:
        test_app.state.config.TTS_ENABLED_VOICES = original


@pytest.mark.tier0
def test_enabled_voices_requires_auth(client, tts_control_configured):
    """The end-user catalog is verified-user gated — anonymous is rejected."""
    resp = client.get("/api/v1/voice-catalog/voices/enabled")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_enabled_voices_allows_plain_user(
    authenticated_user, tts_control_configured, tts_enabled_voices, test_app
):
    """A plain (non-admin) user MAY read the end-user catalog (unlike admin view)."""
    test_app.state.config.TTS_ENABLED_VOICES = {"en-us-amy": True}
    with aioresponses_strict() as m:
        m.get(
            f"{CONTROL_URL}/api/voices",
            status=200,
            payload={"voices": [{"id": "en-us-amy"}, {"id": "en-us-ryan"}]},
        )
        resp = authenticated_user.get("/api/v1/voice-catalog/voices/enabled")
    assert resp.status_code == 200, resp.text


@pytest.mark.tier1
def test_enabled_voices_omits_disabled(
    authenticated_user, tts_control_configured, tts_enabled_voices, test_app
):
    """AC1: only admin-enabled voices appear in the end-user catalog."""
    test_app.state.config.TTS_ENABLED_VOICES = {"en-us-amy": True, "en-us-ryan": False}
    with aioresponses_strict() as m:
        m.get(
            f"{CONTROL_URL}/api/voices",
            status=200,
            payload={"voices": [{"id": "en-us-amy"}, {"id": "en-us-ryan"}]},
        )
        resp = authenticated_user.get("/api/v1/voice-catalog/voices/enabled")
    assert resp.status_code == 200, resp.text
    ids = [v["id"] for v in resp.json()["voices"]]
    assert ids == ["en-us-amy"]


@pytest.mark.tier1
def test_admin_catalog_keeps_disabled_voice(
    authenticated_admin, tts_control_configured, tts_enabled_voices, test_app
):
    """AC2: a disabled voice is still present in the admin-facing catalog."""
    test_app.state.config.TTS_ENABLED_VOICES = {"en-us-amy": True, "en-us-ryan": False}
    with aioresponses_strict() as m:
        m.get(
            f"{CONTROL_URL}/api/voices",
            status=200,
            payload={"voices": [{"id": "en-us-amy"}, {"id": "en-us-ryan"}]},
        )
        resp = authenticated_admin.get("/api/v1/voice-catalog/voices")
    assert resp.status_code == 200, resp.text
    ids = [v["id"] for v in resp.json()["voices"]]
    assert ids == ["en-us-amy", "en-us-ryan"]  # disabled voice still shown to admin


@pytest.mark.tier0
def test_voice_toggle_requires_admin(authenticated_user):
    """Setting a voice's enabled state is an admin curation action."""
    resp = authenticated_user.post(
        "/api/v1/voice-catalog/voices/en-us-amy/enabled", json={"enabled": True}
    )
    assert resp.status_code in (401, 403)


@pytest.mark.tier1
def test_voice_toggle_then_enabled_catalog_reflects_it(
    authenticated_admin, tts_control_configured, tts_enabled_voices
):
    """AC3: enabling a voice makes it appear; disabling removes it — end to end."""
    backend = {"voices": [{"id": "en-us-amy"}, {"id": "en-us-ryan"}]}

    # Initially nothing enabled -> end-user catalog is empty.
    with aioresponses_strict() as m:
        m.get(f"{CONTROL_URL}/api/voices", status=200, payload=backend)
        resp = authenticated_admin.get("/api/v1/voice-catalog/voices/enabled")
    assert resp.json() == {"voices": []}

    # Enable one voice via the toggle.
    resp = authenticated_admin.post(
        "/api/v1/voice-catalog/voices/en-us-amy/enabled", json={"enabled": True}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": "en-us-amy", "enabled": True}

    with aioresponses_strict() as m:
        m.get(f"{CONTROL_URL}/api/voices", status=200, payload=backend)
        resp = authenticated_admin.get("/api/v1/voice-catalog/voices/enabled")
    assert [v["id"] for v in resp.json()["voices"]] == ["en-us-amy"]

    # Disable it again via the toggle.
    resp = authenticated_admin.post(
        "/api/v1/voice-catalog/voices/en-us-amy/enabled", json={"enabled": False}
    )
    assert resp.status_code == 200, resp.text

    with aioresponses_strict() as m:
        m.get(f"{CONTROL_URL}/api/voices", status=200, payload=backend)
        resp = authenticated_admin.get("/api/v1/voice-catalog/voices/enabled")
    assert resp.json() == {"voices": []}


# ---------------------------------------------------------------------------
# R3 — multi-connection aggregation with source attribution (T-012)
#   AC1 — with two TTS connections configured, the aggregate contains voices
#         from both.
#   AC2 — each voice carries the identity of its source connection.
#   AC3 — two similarly-named voices from different connections stay
#         distinguishable by source.
# ---------------------------------------------------------------------------

AGG_URL = "/api/v1/voice-catalog/voices/aggregated"


@pytest.fixture
def audio_connections(test_app):
    """Isolate the saved audio-connection store (AUDIO_CONNECTION_CONFIGS)."""
    original = getattr(test_app.state.config, "AUDIO_CONNECTION_CONFIGS", {})
    test_app.state.config.AUDIO_CONNECTION_CONFIGS = {}
    try:
        yield
    finally:
        test_app.state.config.AUDIO_CONNECTION_CONFIGS = original


@pytest.mark.tier0
def test_aggregated_requires_admin(authenticated_user, tts_control_configured):
    """The aggregated catalog is an admin/management surface."""
    resp = authenticated_user.get(AGG_URL)
    assert resp.status_code in (401, 403)


@pytest.mark.tier1
def test_aggregated_empty_when_no_connections(
    authenticated_admin, audio_connections, tts_control_configured
):
    """No saved self-hosted TTS connections -> an empty aggregate, not a default."""
    resp = authenticated_admin.get(AGG_URL)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"voices": []}


@pytest.mark.tier1
def test_aggregated_two_distinct_backends_merge_with_source(
    authenticated_admin, audio_connections, test_app
):
    """AC1/AC2/AC3: two self-hosted TTS connections pointing at different backends
    (via a per-connection control_base_url) merge into one attributed catalog."""
    test_app.state.config.AUDIO_CONNECTION_CONFIGS = {
        "conn-studio": {
            "type": "self_hosted_tts",
            "fields": {"model": "m", "control_base_url": CONTROL_URL},
        },
        "conn-home": {
            "type": "self_hosted_tts",
            "fields": {"model": "m", "control_base_url": CONTROL_URL_B},
        },
    }
    with aioresponses_strict() as m:
        m.get(
            f"{CONTROL_URL}/api/voices",
            status=200,
            payload={"voices": [{"id": "amy", "name": "Amy"}]},
        )
        m.get(
            f"{CONTROL_URL_B}/api/voices",
            status=200,
            payload={"voices": [{"id": "amy", "name": "Amy"}, {"id": "nova"}]},
        )
        resp = authenticated_admin.get(AGG_URL)

    assert resp.status_code == 200, resp.text
    voices = resp.json()["voices"]
    pairs = [(v["id"], v["source_connection_id"]) for v in voices]
    # AC1: voices from both connections present. AC2: each attributed to source.
    assert pairs == [
        ("amy", "conn-studio"),
        ("amy", "conn-home"),
        ("nova", "conn-home"),
    ]
    # AC3: the two "Amy" voices are distinguishable by source connection.
    amys = [v for v in voices if v["id"] == "amy"]
    assert len(amys) == 2
    assert {v["source_connection_id"] for v in amys} == {"conn-studio", "conn-home"}


@pytest.mark.tier1
def test_aggregated_falls_back_to_shared_control_url(
    authenticated_admin, audio_connections, tts_control_configured, test_app
):
    """Current field set carries no per-connection address, so both connections
    resolve to the shared AUDIO_TTS_CONTROL_BASE_URL — still attributed per source."""
    test_app.state.config.AUDIO_CONNECTION_CONFIGS = {
        "conn-a": {"type": "self_hosted_tts", "fields": {"model": "m", "split_on": "."}},
        "conn-b": {"type": "self_hosted_tts", "fields": {"model": "n", "split_on": "."}},
    }
    with aioresponses_strict() as m:
        # Both connections fall back to the same shared control URL -> two GETs.
        m.get(f"{CONTROL_URL}/api/voices", status=200, payload={"voices": [{"id": "amy"}]})
        m.get(f"{CONTROL_URL}/api/voices", status=200, payload={"voices": [{"id": "amy"}]})
        resp = authenticated_admin.get(AGG_URL)

    assert resp.status_code == 200, resp.text
    pairs = [(v["id"], v["source_connection_id"]) for v in resp.json()["voices"]]
    assert pairs == [("amy", "conn-a"), ("amy", "conn-b")]


@pytest.mark.tier1
def test_aggregated_ignores_non_self_hosted_and_unreachable(
    authenticated_admin, audio_connections, test_app
):
    """Only self-hosted TTS connections are aggregated; a hosted connection is
    skipped, and an unreachable self-hosted one contributes nothing (no failure)."""
    test_app.state.config.TTS_CONTROL_BASE_URL = ""
    test_app.state.config.AUDIO_CONNECTION_CONFIGS = {
        "hosted": {"type": "hosted_general", "fields": {"stt_base_url": "http://x"}},
        "reachable": {
            "type": "self_hosted_tts",
            "fields": {"model": "m", "control_base_url": CONTROL_URL},
        },
        "down": {
            "type": "self_hosted_tts",
            "fields": {"model": "m", "control_base_url": CONTROL_URL_B},
        },
    }
    with aioresponses_strict() as m:
        m.get(f"{CONTROL_URL}/api/voices", status=200, payload={"voices": [{"id": "amy"}]})
        m.get(f"{CONTROL_URL_B}/api/voices", status=503, payload={"error": "down"})
        resp = authenticated_admin.get(AGG_URL)

    assert resp.status_code == 200, resp.text
    pairs = [(v["id"], v["source_connection_id"]) for v in resp.json()["voices"]]
    assert pairs == [("amy", "reachable")]


# ---------------------------------------------------------------------------
# R1 (cavekit-audio-voice-picker) — admin voice-curation surface (T-015)
#   AC1 — the surface lists every voice in the aggregated catalog, incl. disabled.
#   AC2 — each listed voice carries a toggle (enabled flag + the toggle endpoint).
#   AC3 — the list is searchable, per the text-model curation pattern.
#   AC4 — a toggle persists and is reflected on reload of the surface.
# ---------------------------------------------------------------------------

CURATION_URL = "/api/v1/voice-catalog/voices/curation"


@pytest.mark.tier0
def test_curation_requires_admin(authenticated_user, tts_control_configured):
    """The curation surface is an admin/management action — a plain user is out."""
    resp = authenticated_user.get(CURATION_URL)
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_curation_unauthenticated_rejected(client, tts_control_configured):
    resp = client.get(CURATION_URL)
    assert resp.status_code in (401, 403)


@pytest.mark.tier1
def test_curation_lists_every_voice_including_disabled(
    authenticated_admin, audio_connections, tts_enabled_voices, test_app
):
    """AC1/AC2: every aggregated voice is listed with an enabled flag, and a
    disabled voice is still present (not filtered out) for curation."""
    test_app.state.config.TTS_ENABLED_VOICES = {"amy": True, "ryan": False}
    test_app.state.config.AUDIO_CONNECTION_CONFIGS = {
        "conn-studio": {
            "type": "self_hosted_tts",
            "fields": {"model": "m", "control_base_url": CONTROL_URL},
        },
    }
    with aioresponses_strict() as m:
        m.get(
            f"{CONTROL_URL}/api/voices",
            status=200,
            payload={"voices": [{"id": "amy"}, {"id": "ryan"}, {"id": "nova"}]},
        )
        resp = authenticated_admin.get(CURATION_URL)

    assert resp.status_code == 200, resp.text
    voices = {v["id"]: v for v in resp.json()["voices"]}
    # AC1: all three listed, disabled 'ryan' and never-set 'nova' included.
    assert set(voices) == {"amy", "ryan", "nova"}
    # AC2: each carries an enabled flag reflecting the admin map.
    assert voices["amy"]["enabled"] is True
    assert voices["ryan"]["enabled"] is False
    assert voices["nova"]["enabled"] is False
    # Source attribution is preserved on the curation surface (R4/T-018).
    assert voices["amy"]["source_connection_id"] == "conn-studio"


@pytest.mark.tier1
def test_curation_search_narrows_the_list(
    authenticated_admin, audio_connections, tts_enabled_voices, test_app
):
    """AC3: the ?search= query param filters the curation list."""
    test_app.state.config.AUDIO_CONNECTION_CONFIGS = {
        "conn-studio": {
            "type": "self_hosted_tts",
            "fields": {"model": "m", "control_base_url": CONTROL_URL},
        },
    }
    backend = {
        "voices": [
            {"id": "en-us-amy", "name": "Amy", "language": "en-US"},
            {"id": "de-de-max", "name": "Max", "language": "de-DE"},
        ]
    }
    with aioresponses_strict() as m:
        m.get(f"{CONTROL_URL}/api/voices", status=200, payload=backend)
        resp = authenticated_admin.get(CURATION_URL, params={"search": "max"})

    assert resp.status_code == 200, resp.text
    assert [v["id"] for v in resp.json()["voices"]] == ["de-de-max"]


@pytest.mark.tier1
def test_curation_toggle_persists_and_reflects_on_reload(
    authenticated_admin, audio_connections, tts_enabled_voices, test_app
):
    """AC4: toggling a voice persists and is reflected when the surface reloads.

    Reads the curation surface, flips a voice via POST /voices/{id}/enabled, then
    re-reads the surface and sees the new enabled state — the toggle and the list
    share the persisted AUDIO_TTS_ENABLED_VOICES map."""
    test_app.state.config.AUDIO_CONNECTION_CONFIGS = {
        "conn-studio": {
            "type": "self_hosted_tts",
            "fields": {"model": "m", "control_base_url": CONTROL_URL},
        },
    }
    backend = {"voices": [{"id": "amy"}, {"id": "ryan"}]}

    # Initial load: nothing enabled.
    with aioresponses_strict() as m:
        m.get(f"{CONTROL_URL}/api/voices", status=200, payload=backend)
        resp = authenticated_admin.get(CURATION_URL)
    flags = {v["id"]: v["enabled"] for v in resp.json()["voices"]}
    assert flags == {"amy": False, "ryan": False}

    # Toggle amy on via the (T-014) toggle endpoint.
    resp = authenticated_admin.post(
        "/api/v1/voice-catalog/voices/amy/enabled", json={"enabled": True}
    )
    assert resp.status_code == 200, resp.text

    # Reload the curation surface: amy now reads enabled, ryan still off, both listed.
    with aioresponses_strict() as m:
        m.get(f"{CONTROL_URL}/api/voices", status=200, payload=backend)
        resp = authenticated_admin.get(CURATION_URL)
    flags = {v["id"]: v["enabled"] for v in resp.json()["voices"]}
    assert flags == {"amy": True, "ryan": False}
    # The change is on the persisted config map, not request-local state.
    assert test_app.state.config.TTS_ENABLED_VOICES.get("amy") is True

    # Toggle amy back off -> reflected on the next reload.
    resp = authenticated_admin.post(
        "/api/v1/voice-catalog/voices/amy/enabled", json={"enabled": False}
    )
    assert resp.status_code == 200, resp.text
    with aioresponses_strict() as m:
        m.get(f"{CONTROL_URL}/api/voices", status=200, payload=backend)
        resp = authenticated_admin.get(CURATION_URL)
    flags = {v["id"]: v["enabled"] for v in resp.json()["voices"]}
    assert flags == {"amy": False, "ryan": False}


# ---------------------------------------------------------------------------
# R2 (cavekit-audio-voice-picker) — end-user voice picker honors admin curation
# (T-016)
#   AC1 — the end-user picker offers only voices currently enabled by an admin.
#   AC2 — an admin-disabled voice is not selectable in the end-user picker.
#   AC3 — enabling a voice in admin curation makes it available; disabling removes
#         it — and this holds over the SAME aggregated catalog admin curates, so a
#         voice on any saved self-hosted TTS connection is covered (not only the
#         default single connection ``GET /voices/enabled`` sees).
# ---------------------------------------------------------------------------

SELECTABLE_URL = "/api/v1/voice-catalog/voices/selectable"


@pytest.mark.tier0
def test_selectable_requires_auth(client, tts_control_configured):
    """The end-user picker is verified-user gated — anonymous is rejected."""
    resp = client.get(SELECTABLE_URL)
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_selectable_allows_plain_user(
    authenticated_user, audio_connections, tts_enabled_voices, test_app
):
    """A plain (non-admin) user MAY read the picker (unlike the admin curation view)."""
    test_app.state.config.TTS_ENABLED_VOICES = {"amy": True}
    test_app.state.config.AUDIO_CONNECTION_CONFIGS = {
        "conn-studio": {
            "type": "self_hosted_tts",
            "fields": {"model": "m", "control_base_url": CONTROL_URL},
        },
    }
    with aioresponses_strict() as m:
        m.get(
            f"{CONTROL_URL}/api/voices",
            status=200,
            payload={"voices": [{"id": "amy"}, {"id": "ryan"}]},
        )
        resp = authenticated_user.get(SELECTABLE_URL)
    assert resp.status_code == 200, resp.text


@pytest.mark.tier1
def test_selectable_offers_only_enabled_voices(
    authenticated_user, audio_connections, tts_enabled_voices, test_app
):
    """AC1/AC2: only admin-enabled voices are offered; a disabled voice is absent."""
    test_app.state.config.TTS_ENABLED_VOICES = {"amy": True, "ryan": False}
    test_app.state.config.AUDIO_CONNECTION_CONFIGS = {
        "conn-studio": {
            "type": "self_hosted_tts",
            "fields": {"model": "m", "control_base_url": CONTROL_URL},
        },
    }
    with aioresponses_strict() as m:
        m.get(
            f"{CONTROL_URL}/api/voices",
            status=200,
            # 'ryan' disabled, 'nova' never enabled — neither is selectable.
            payload={"voices": [{"id": "amy"}, {"id": "ryan"}, {"id": "nova"}]},
        )
        resp = authenticated_user.get(SELECTABLE_URL)

    assert resp.status_code == 200, resp.text
    ids = [v["id"] for v in resp.json()["voices"]]
    assert ids == ["amy"]  # AC1: only enabled; AC2: disabled/never-set excluded


@pytest.mark.tier1
def test_selectable_covers_voice_on_second_connection(
    authenticated_user, audio_connections, tts_enabled_voices, test_app
):
    """AC3 (multi-connection): a voice living only on a SECOND self-hosted TTS
    connection — one the single-connection GET /voices/enabled never sees — is
    still offered once an admin enables it, because the picker filters the SAME
    aggregated catalog the admin curates. This is the gap T-016 closes over
    T-014's single-connection enabled view."""
    test_app.state.config.TTS_CONTROL_BASE_URL = ""  # no default single connection
    test_app.state.config.TTS_ENABLED_VOICES = {"beta-only": True}
    test_app.state.config.AUDIO_CONNECTION_CONFIGS = {
        "conn-a": {
            "type": "self_hosted_tts",
            "fields": {"model": "m", "control_base_url": CONTROL_URL},
        },
        "conn-b": {
            "type": "self_hosted_tts",
            "fields": {"model": "m", "control_base_url": CONTROL_URL_B},
        },
    }
    with aioresponses_strict() as m:
        m.get(f"{CONTROL_URL}/api/voices", status=200, payload={"voices": [{"id": "alpha"}]})
        m.get(
            f"{CONTROL_URL_B}/api/voices",
            status=200,
            payload={"voices": [{"id": "beta-only", "name": "Beta"}]},
        )
        resp = authenticated_user.get(SELECTABLE_URL)

    assert resp.status_code == 200, resp.text
    voices = resp.json()["voices"]
    ids = [v["id"] for v in voices]
    # The enabled voice from the second connection is offered and stays attributed.
    assert ids == ["beta-only"]
    assert voices[0]["source_connection_id"] == "conn-b"


@pytest.mark.tier1
def test_selectable_empty_when_nothing_enabled(
    authenticated_user, audio_connections, tts_enabled_voices, test_app
):
    """Nothing enabled -> an empty picker catalog (never a placeholder/default)."""
    test_app.state.config.AUDIO_CONNECTION_CONFIGS = {
        "conn-studio": {
            "type": "self_hosted_tts",
            "fields": {"model": "m", "control_base_url": CONTROL_URL},
        },
    }
    with aioresponses_strict() as m:
        m.get(
            f"{CONTROL_URL}/api/voices",
            status=200,
            payload={"voices": [{"id": "amy"}, {"id": "ryan"}]},
        )
        resp = authenticated_user.get(SELECTABLE_URL)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"voices": []}


@pytest.mark.tier1
def test_curation_toggle_then_selectable_reflects_it(
    authenticated_admin, audio_connections, tts_enabled_voices, test_app
):
    """AC3 end-to-end: enabling a voice in admin curation makes it appear in the
    end-user picker; disabling it removes it. The admin toggles against the same
    aggregated catalog the picker reads, and both share the one enabled map.

    Uses the admin client throughout — an admin also satisfies the picker's
    verified-user gate, and the ``authenticated_admin``/``authenticated_user``
    fixtures share one underlying test client (their Authorization headers would
    clobber each other), so a single client is used. That a *plain* user may read
    the picker is proven separately by ``test_selectable_allows_plain_user``."""
    test_app.state.config.AUDIO_CONNECTION_CONFIGS = {
        "conn-studio": {
            "type": "self_hosted_tts",
            "fields": {"model": "m", "control_base_url": CONTROL_URL},
        },
    }
    backend = {"voices": [{"id": "amy"}, {"id": "ryan"}]}

    # Initially nothing enabled -> the end-user picker is empty.
    with aioresponses_strict() as m:
        m.get(f"{CONTROL_URL}/api/voices", status=200, payload=backend)
        resp = authenticated_admin.get(SELECTABLE_URL)
    assert resp.json() == {"voices": []}

    # Admin enables 'amy' via the curation toggle.
    resp = authenticated_admin.post(
        "/api/v1/voice-catalog/voices/amy/enabled", json={"enabled": True}
    )
    assert resp.status_code == 200, resp.text

    # The end-user picker now offers 'amy' (and only 'amy').
    with aioresponses_strict() as m:
        m.get(f"{CONTROL_URL}/api/voices", status=200, payload=backend)
        resp = authenticated_admin.get(SELECTABLE_URL)
    assert [v["id"] for v in resp.json()["voices"]] == ["amy"]

    # Admin disables 'amy' again -> it drops out of the picker.
    resp = authenticated_admin.post(
        "/api/v1/voice-catalog/voices/amy/enabled", json={"enabled": False}
    )
    assert resp.status_code == 200, resp.text
    with aioresponses_strict() as m:
        m.get(f"{CONTROL_URL}/api/voices", status=200, payload=backend)
        resp = authenticated_admin.get(SELECTABLE_URL)
    assert resp.json() == {"voices": []}
# R4 (cavekit-audio-voice-picker) — source connection shown in curation (T-018)
#   AC1 — each voice in the curation list displays its source connection.
#   AC2 — two similarly-named voices from different connections are
#         distinguishable by their SHOWN source.
# ---------------------------------------------------------------------------


@pytest.mark.tier1
def test_curation_shows_human_source_connection_label(
    authenticated_admin, audio_connections, tts_enabled_voices, test_app
):
    """AC1: each voice displays a human-readable source_connection_label derived
    from its connection (type label + model), not just the opaque connection id."""
    test_app.state.config.AUDIO_CONNECTION_CONFIGS = {
        "conn-studio": {
            "type": "self_hosted_tts",
            "fields": {"model": "glados", "control_base_url": CONTROL_URL},
        },
    }
    with aioresponses_strict() as m:
        m.get(
            f"{CONTROL_URL}/api/voices",
            status=200,
            payload={"voices": [{"id": "amy", "name": "Amy"}]},
        )
        resp = authenticated_admin.get(CURATION_URL)

    assert resp.status_code == 200, resp.text
    voice = resp.json()["voices"][0]
    # The opaque id is still carried (authoritative identity)…
    assert voice["source_connection_id"] == "conn-studio"
    # …and a meaningful, human-readable source label is shown for display.
    assert voice["source_connection_label"] == "Self-hosted TTS (glados)"


@pytest.mark.tier1
def test_curation_similarly_named_voices_distinguishable_by_shown_source(
    authenticated_admin, audio_connections, tts_enabled_voices, test_app
):
    """AC2: two "Amy" voices from two connections show two distinct sources.

    Even when the two connections carry an identical base label (same type and
    model), the shown source is disambiguated per connection, so an admin can tell
    the two similarly-named voices apart by their shown source alone."""
    test_app.state.config.AUDIO_CONNECTION_CONFIGS = {
        "conn-studio": {
            "type": "self_hosted_tts",
            "fields": {"model": "m", "control_base_url": CONTROL_URL},
        },
        "conn-home": {
            "type": "self_hosted_tts",
            "fields": {"model": "m", "control_base_url": CONTROL_URL_B},
        },
    }
    with aioresponses_strict() as m:
        m.get(
            f"{CONTROL_URL}/api/voices",
            status=200,
            payload={"voices": [{"id": "amy", "name": "Amy"}]},
        )
        m.get(
            f"{CONTROL_URL_B}/api/voices",
            status=200,
            payload={"voices": [{"id": "amy", "name": "Amy"}]},
        )
        resp = authenticated_admin.get(CURATION_URL)

    assert resp.status_code == 200, resp.text
    voices = resp.json()["voices"]
    # Both voices share the id and name…
    assert [v["id"] for v in voices] == ["amy", "amy"]
    assert {v["name"] for v in voices} == {"Amy"}
    # …yet each shows a DISTINCT source connection label (R4 AC2).
    shown = [v["source_connection_label"] for v in voices]
    assert shown[0] != shown[1]
    assert set(shown) == {
        "Self-hosted TTS (m) #conn-studio",
        "Self-hosted TTS (m) #conn-home",
    }


# ---------------------------------------------------------------------------
# R3 (cavekit-audio-voice-picker) — Selection Uses Real Metadata (T-017)
#   AC1 — each selectable voice is presented with its name, language and gender.
#   AC2 — selection is made from a real selector (a list of structured,
#         metadata-bearing voice objects), not a free-text entry field.
#   AC3 — no free-text fallback path for entering a voice remains in the
#         end-user picker: the picker only offers real catalog entries, and a
#         voice id absent from the real catalog cannot be conjured in.
#
# The end-user picker's sole API data source is GET /voices/selectable (T-016).
# These tests prove the response shape a real selector consumes always carries
# full name/language/gender metadata and is always a structured object — so the
# selector never needs to degrade to the prior free-text-with-autocomplete input.
# The legacy id+name GET /audio/voices (routers/audio.py, driven by the flat
# TTS_ENGINE serving config) is the deliberately-preserved wire-contract/serving
# path guarded by R5/T-019 — it is NOT the R3 picker surface and is left untouched.
# ---------------------------------------------------------------------------


@pytest.mark.tier1
def test_selectable_presents_full_metadata_per_voice(
    authenticated_user, audio_connections, tts_enabled_voices, test_app
):
    """AC1: every voice the end-user picker offers carries name, language and
    gender — the real-metadata a selector renders, not a bare identifier."""
    test_app.state.config.TTS_ENABLED_VOICES = {"en-us-amy": True, "de-de-max": True}
    test_app.state.config.AUDIO_CONNECTION_CONFIGS = {
        "conn-studio": {
            "type": "self_hosted_tts",
            "fields": {"model": "m", "control_base_url": CONTROL_URL},
        },
    }
    with aioresponses_strict() as m:
        m.get(
            f"{CONTROL_URL}/api/voices",
            status=200,
            payload={
                "voices": [
                    {"id": "en-us-amy", "name": "Amy", "language": "en-US", "gender": "female"},
                    {"id": "de-de-max", "name": "Max", "language": "de-DE", "gender": "male"},
                ]
            },
        )
        resp = authenticated_user.get(SELECTABLE_URL)

    assert resp.status_code == 200, resp.text
    voices = {v["id"]: v for v in resp.json()["voices"]}
    assert set(voices) == {"en-us-amy", "de-de-max"}
    for entry in voices.values():
        # AC1: name/language/gender are all present on every offered voice.
        assert {"id", "name", "language", "gender"} <= set(entry.keys())
        assert isinstance(entry["name"], str) and entry["name"]
    assert voices["en-us-amy"]["name"] == "Amy"
    assert voices["en-us-amy"]["language"] == "en-US"
    assert voices["en-us-amy"]["gender"] == "female"
    assert voices["de-de-max"]["language"] == "de-DE"
    assert voices["de-de-max"]["gender"] == "male"


@pytest.mark.tier1
def test_selectable_voice_is_structured_object_not_bare_identifier(
    authenticated_user, audio_connections, tts_enabled_voices, test_app
):
    """AC1/AC2: even a backend-nameless voice is offered as a structured object
    with a name (its id) and explicit null language/gender — so the selector
    always has a real row to render and never falls back to a free-text id box."""
    test_app.state.config.TTS_ENABLED_VOICES = {"bare-voice": True}
    test_app.state.config.AUDIO_CONNECTION_CONFIGS = {
        "conn-studio": {
            "type": "self_hosted_tts",
            "fields": {"model": "m", "control_base_url": CONTROL_URL},
        },
    }
    with aioresponses_strict() as m:
        m.get(
            f"{CONTROL_URL}/api/voices",
            status=200,
            payload={"voices": [{"id": "bare-voice"}]},
        )
        resp = authenticated_user.get(SELECTABLE_URL)

    assert resp.status_code == 200, resp.text
    entry = resp.json()["voices"][0]
    # AC2: a structured selector row (dict of fields), never a bare id string.
    assert isinstance(entry, dict)
    assert entry["id"] == "bare-voice"
    assert entry["name"] == "bare-voice"  # name always present (R2 AC4)
    assert entry["language"] is None
    assert entry["gender"] is None
    assert {"id", "name", "language", "gender"} <= set(entry.keys())


@pytest.mark.tier1
def test_selectable_only_offers_real_catalog_voices_no_free_text_injection(
    authenticated_user, audio_connections, tts_enabled_voices, test_app
):
    """AC3: no free-text/bare-id fallback — a voice id enabled by an admin but
    absent from the real backend catalog is NOT offered by the picker. The picker
    can only present metadata-bearing voices the backend actually reports, so an
    arbitrary free-text voice can never be entered through this surface."""
    test_app.state.config.TTS_ENABLED_VOICES = {"real-voice": True, "ghost": True}
    test_app.state.config.AUDIO_CONNECTION_CONFIGS = {
        "conn-studio": {
            "type": "self_hosted_tts",
            "fields": {"model": "m", "control_base_url": CONTROL_URL},
        },
    }
    with aioresponses_strict() as m:
        m.get(
            f"{CONTROL_URL}/api/voices",
            status=200,
            # backend reports 'real-voice' only — 'ghost' is enabled but not real.
            payload={"voices": [{"id": "real-voice", "name": "Real", "language": "en"}]},
        )
        resp = authenticated_user.get(SELECTABLE_URL)

    assert resp.status_code == 200, resp.text
    ids = [v["id"] for v in resp.json()["voices"]]
    assert ids == ["real-voice"]  # 'ghost' cannot be conjured into the picker
    assert "ghost" not in ids


@pytest.mark.tier1
def test_selectable_endpoint_is_read_only_no_free_text_submission(
    authenticated_user, tts_control_configured
):
    """AC2/AC3: the end-user picker surface is a READ of a structured catalog only.

    Selection is made by choosing from the metadata-bearing list GET
    /voices/selectable returns; the endpoint accepts no request body and rejects a
    write (POST) attempt with 405 Method Not Allowed. There is therefore no
    free-text/arbitrary-voice submission path on the picker surface — a user cannot
    POST a free-text voice string here to select it (the only mutating voice route
    is the admin-gated enable/disable toggle, exercised elsewhere)."""
    resp = authenticated_user.post(SELECTABLE_URL, json={"voice": "anything-freetext"})
    assert resp.status_code == 405  # method not allowed: read-only selector surface
