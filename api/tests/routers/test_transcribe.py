"""Transcribe router endpoint tests (cavekit-audio-transcribe-router R1).

The listing endpoint talks to the self-hosted STT backend's control port over
aiohttp; upstream is mocked with aioresponses. Covers auth gating, the
unconfigured-backend guard, and the downloaded-vs-pullable listing contract.
"""

import json

import pytest

from tests.mocks.external_services import aioresponses_strict

CONTROL_URL = "http://self-transcribe:9000"


@pytest.fixture
def stt_control_configured(test_app):
    """Point the router at a self-hosted STT control backend for the test."""
    original = getattr(test_app.state.config, "STT_CONTROL_BASE_URL", "")
    test_app.state.config.STT_CONTROL_BASE_URL = CONTROL_URL
    try:
        yield
    finally:
        test_app.state.config.STT_CONTROL_BASE_URL = original


@pytest.mark.tier0
def test_transcribe_models_requires_admin(authenticated_user, stt_control_configured):
    """Listing is an admin management surface — a plain user is rejected."""
    resp = authenticated_user.get("/api/v1/transcribe/models")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_transcribe_models_unauthenticated_rejected(client, stt_control_configured):
    resp = client.get("/api/v1/transcribe/models")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_transcribe_models_unconfigured_backend(authenticated_admin, test_app):
    """No control URL configured -> 400 (client/config condition)."""
    original = getattr(test_app.state.config, "STT_CONTROL_BASE_URL", "")
    test_app.state.config.STT_CONTROL_BASE_URL = ""
    try:
        resp = authenticated_admin.get("/api/v1/transcribe/models")
        assert resp.status_code == 400
    finally:
        test_app.state.config.STT_CONTROL_BASE_URL = original


@pytest.mark.tier1
def test_transcribe_models_lists_downloaded_and_pullable(authenticated_admin, stt_control_configured):
    """AC1+AC2+AC3: downloaded-ready and pullable both reported, distinguished."""
    with aioresponses_strict() as m:
        m.get(
            f"{CONTROL_URL}/api/models",
            status=200,
            payload={"data": [{"id": "whisper-small", "status": {"value": "loaded"}}]},
        )
        m.get(
            f"{CONTROL_URL}/api/models/available",
            status=200,
            payload=[
                {"id": "whisper-small"},  # already downloaded
                {"id": "whisper-large-v3", "description": "Large multilingual"},
            ],
        )
        resp = authenticated_admin.get("/api/v1/transcribe/models")

    assert resp.status_code == 200, resp.text
    by_id = {e["id"]: e for e in resp.json()["data"]}

    assert by_id["whisper-small"]["availability"] == "downloaded"
    assert by_id["whisper-small"]["downloaded"] is True
    assert by_id["whisper-small"]["status"] == "loaded"

    assert by_id["whisper-large-v3"]["availability"] == "pullable"
    assert by_id["whisper-large-v3"]["downloaded"] is False
    assert by_id["whisper-large-v3"]["description"] == "Large multilingual"


@pytest.mark.tier1
def test_transcribe_models_catalog_unreachable_still_lists_downloaded(
    authenticated_admin, stt_control_configured
):
    """Catalog view down but downloaded view up -> still report downloaded."""
    with aioresponses_strict() as m:
        m.get(
            f"{CONTROL_URL}/api/models",
            status=200,
            payload=[{"id": "whisper-base"}],
        )
        m.get(f"{CONTROL_URL}/api/models/available", status=503, payload={"error": "down"})
        resp = authenticated_admin.get("/api/v1/transcribe/models")

    assert resp.status_code == 200, resp.text
    by_id = {e["id"]: e for e in resp.json()["data"]}
    assert by_id["whisper-base"]["availability"] == "downloaded"


@pytest.mark.tier1
def test_transcribe_models_empty_backend(authenticated_admin, stt_control_configured):
    """Fresh backend: both views empty -> empty listing, 200."""
    with aioresponses_strict() as m:
        m.get(f"{CONTROL_URL}/api/models", status=200, payload={"data": []})
        m.get(f"{CONTROL_URL}/api/models/available", status=200, payload=[])
        resp = authenticated_admin.get("/api/v1/transcribe/models")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"data": []}


@pytest.mark.tier1
def test_transcribe_models_backend_fully_unreachable(authenticated_admin, stt_control_configured):
    """Both control views unreachable -> 502 (upstream fault, not silent empty)."""
    with aioresponses_strict() as m:
        m.get(f"{CONTROL_URL}/api/models", status=503, payload={"error": "down"})
        m.get(f"{CONTROL_URL}/api/models/available", status=503, payload={"error": "down"})
        resp = authenticated_admin.get("/api/v1/transcribe/models")

    assert resp.status_code == 502


# ---------------------------------------------------------------------------
# R4: Active model swap (POST /models/{id}/activate)
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_transcribe_activate_requires_admin(authenticated_user, stt_control_configured):
    """Swap is an admin management mutation — a plain user is rejected."""
    resp = authenticated_user.post("/api/v1/transcribe/models/whisper-small/activate")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_transcribe_activate_unauthenticated_rejected(client, stt_control_configured):
    resp = client.post("/api/v1/transcribe/models/whisper-small/activate")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_transcribe_activate_unconfigured_backend(authenticated_admin, test_app):
    """No control URL configured -> 400 (client/config condition)."""
    original = getattr(test_app.state.config, "STT_CONTROL_BASE_URL", "")
    test_app.state.config.STT_CONTROL_BASE_URL = ""
    try:
        resp = authenticated_admin.post("/api/v1/transcribe/models/whisper-small/activate")
        assert resp.status_code == 400
    finally:
        test_app.state.config.STT_CONTROL_BASE_URL = original


@pytest.mark.tier1
def test_transcribe_activate_downloaded_model_succeeds(authenticated_admin, stt_control_configured):
    """AC1+AC2: a downloaded model is made active via a live control-port load,
    no process restart; the newly active model is reported back."""
    with aioresponses_strict() as m:
        m.get(
            f"{CONTROL_URL}/api/models",
            status=200,
            payload={"data": [{"id": "whisper-small", "status": {"value": "unloaded"}}]},
        )
        m.get(
            f"{CONTROL_URL}/api/models/available",
            status=200,
            payload=[{"id": "whisper-small"}, {"id": "whisper-large-v3"}],
        )
        m.post(f"{CONTROL_URL}/api/models/load", status=200, payload={"ok": True})
        resp = authenticated_admin.post("/api/v1/transcribe/models/whisper-small/activate")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["active"] == "whisper-small"


@pytest.mark.tier1
def test_transcribe_activate_pullable_only_rejected(authenticated_admin, stt_control_configured):
    """AC3: a merely-pullable (not downloaded) model cannot be made active."""
    with aioresponses_strict() as m:
        m.get(
            f"{CONTROL_URL}/api/models",
            status=200,
            payload={"data": [{"id": "whisper-small"}]},
        )
        m.get(
            f"{CONTROL_URL}/api/models/available",
            status=200,
            payload=[{"id": "whisper-small"}, {"id": "whisper-large-v3"}],
        )
        # No m.post registered: a rejected swap must never reach the backend.
        resp = authenticated_admin.post("/api/v1/transcribe/models/whisper-large-v3/activate")

    assert resp.status_code == 404, resp.text


@pytest.mark.tier1
def test_transcribe_activate_unknown_model_rejected(authenticated_admin, stt_control_configured):
    """AC3: a model in neither view cannot be made active."""
    with aioresponses_strict() as m:
        m.get(f"{CONTROL_URL}/api/models", status=200, payload={"data": [{"id": "whisper-small"}]})
        m.get(f"{CONTROL_URL}/api/models/available", status=200, payload=[{"id": "whisper-small"}])
        resp = authenticated_admin.post("/api/v1/transcribe/models/nope/activate")

    assert resp.status_code == 404, resp.text


@pytest.mark.tier1
def test_transcribe_activate_backend_unreachable(authenticated_admin, stt_control_configured):
    """Listing views unreachable -> 502, swap never silently no-ops."""
    with aioresponses_strict() as m:
        m.get(f"{CONTROL_URL}/api/models", status=503, payload={"error": "down"})
        m.get(f"{CONTROL_URL}/api/models/available", status=503, payload={"error": "down"})
        resp = authenticated_admin.post("/api/v1/transcribe/models/whisper-small/activate")

    assert resp.status_code == 502, resp.text


@pytest.mark.tier1
def test_transcribe_activate_load_command_failure_surfaces(authenticated_admin, stt_control_configured):
    """A valid target whose backend load command fails surfaces as 502."""
    with aioresponses_strict() as m:
        m.get(f"{CONTROL_URL}/api/models", status=200, payload={"data": [{"id": "whisper-small"}]})
        m.get(f"{CONTROL_URL}/api/models/available", status=200, payload=[{"id": "whisper-small"}])
        m.post(f"{CONTROL_URL}/api/models/load", status=500, payload={"error": "boom"})
        resp = authenticated_admin.post("/api/v1/transcribe/models/whisper-small/activate")

    assert resp.status_code == 502, resp.text


# ---------------------------------------------------------------------------
# Admin curation surface (cavekit-audio-transcribe-picker R1)
# ---------------------------------------------------------------------------


@pytest.fixture
def stt_enabled_models_isolated(test_app):
    """Isolate the admin-set enabled-models map so tests don't leak state."""
    original = getattr(test_app.state.config, "STT_ENABLED_MODELS", {})
    test_app.state.config.STT_ENABLED_MODELS = {}
    try:
        yield
    finally:
        test_app.state.config.STT_ENABLED_MODELS = dict(original) if isinstance(original, dict) else {}


def _mock_catalog(m):
    """Two-entry catalog: one downloaded, one merely pullable."""
    m.get(
        f"{CONTROL_URL}/api/models",
        status=200,
        payload={"data": [{"id": "whisper-small", "status": {"value": "loaded"}}]},
    )
    m.get(
        f"{CONTROL_URL}/api/models/available",
        status=200,
        payload=[
            {"id": "whisper-small"},
            {"id": "whisper-large-v3", "description": "Large multilingual"},
        ],
    )


@pytest.mark.tier1
def test_curation_lists_every_model_with_enabled_flag(
    authenticated_admin, stt_control_configured, stt_enabled_models_isolated
):
    """AC1+AC2: every model (downloaded and pullable) is listed, each with a
    toggle-backing ``enabled`` flag (default disabled)."""
    with aioresponses_strict() as m:
        _mock_catalog(m)
        resp = authenticated_admin.get("/api/v1/transcribe/models")

    assert resp.status_code == 200, resp.text
    by_id = {e["id"]: e for e in resp.json()["data"]}
    assert set(by_id) == {"whisper-small", "whisper-large-v3"}
    assert by_id["whisper-small"]["enabled"] is False
    assert by_id["whisper-large-v3"]["enabled"] is False


@pytest.mark.tier1
def test_curation_toggle_requires_admin(
    authenticated_user, stt_control_configured, stt_enabled_models_isolated
):
    """The toggle is an admin management action — a plain user is rejected."""
    resp = authenticated_user.post(
        "/api/v1/transcribe/models/whisper-small/enabled", json={"enabled": True}
    )
    assert resp.status_code in (401, 403)


@pytest.mark.tier1
def test_curation_toggle_persists_and_reflects_on_reload(
    authenticated_admin, stt_control_configured, stt_enabled_models_isolated
):
    """AC4: toggling enabled persists and a fresh listing reflects the new state."""
    # Enable a downloaded model.
    resp = authenticated_admin.post(
        "/api/v1/transcribe/models/whisper-small/enabled", json={"enabled": True}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": "whisper-small", "enabled": True}

    # A subsequent listing (a "reload") reflects the persisted flag.
    with aioresponses_strict() as m:
        _mock_catalog(m)
        listing = authenticated_admin.get("/api/v1/transcribe/models")
    by_id = {e["id"]: e for e in listing.json()["data"]}
    assert by_id["whisper-small"]["enabled"] is True

    # Disabling flips it back.
    authenticated_admin.post(
        "/api/v1/transcribe/models/whisper-small/enabled", json={"enabled": False}
    )
    with aioresponses_strict() as m:
        _mock_catalog(m)
        listing = authenticated_admin.get("/api/v1/transcribe/models")
    by_id = {e["id"]: e for e in listing.json()["data"]}
    assert by_id["whisper-small"]["enabled"] is False


@pytest.mark.tier1
def test_curation_can_enable_pullable_model(
    authenticated_admin, stt_control_configured, stt_enabled_models_isolated
):
    """A model may be enabled while merely pullable (not yet downloaded)."""
    resp = authenticated_admin.post(
        "/api/v1/transcribe/models/whisper-large-v3/enabled", json={"enabled": True}
    )
    assert resp.status_code == 200, resp.text

    with aioresponses_strict() as m:
        _mock_catalog(m)
        listing = authenticated_admin.get("/api/v1/transcribe/models")
    entry = {e["id"]: e for e in listing.json()["data"]}["whisper-large-v3"]
    assert entry["downloaded"] is False
    assert entry["availability"] == "pullable"
    assert entry["enabled"] is True


@pytest.mark.tier1
def test_curation_toggle_survives_backend_unreachable(
    authenticated_admin, stt_control_configured, stt_enabled_models_isolated
):
    """The toggle records intent by id and does not require a reachable backend."""
    resp = authenticated_admin.post(
        "/api/v1/transcribe/models/whisper-small/enabled", json={"enabled": True}
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.tier1
def test_curation_list_is_searchable(
    authenticated_admin, stt_control_configured, stt_enabled_models_isolated
):
    """AC3: the listing accepts a search param filtering by id/name/description."""
    with aioresponses_strict() as m:
        _mock_catalog(m)
        resp = authenticated_admin.get("/api/v1/transcribe/models", params={"search": "large"})

    assert resp.status_code == 200, resp.text
    ids = {e["id"] for e in resp.json()["data"]}
    assert ids == {"whisper-large-v3"}


# ---------------------------------------------------------------------------
# Downloaded-vs-pullable distinction in the curation list
# (cavekit-audio-transcribe-picker R2 — T-029)
#
# R2 asks that a downloaded-and-ready model be visibly distinct from a merely
# pullable one *in the curation list itself*, legible without opening a separate
# management view. The admin curation endpoint (GET /models, with its enabled
# overlay) is that curation list; these tests assert the distinction is present
# on that very response — every entry carrying downloaded + availability
# alongside the curation enabled flag, in one round-trip.
# ---------------------------------------------------------------------------


@pytest.mark.tier1
def test_curation_list_marks_downloaded_vs_pullable(
    authenticated_admin, stt_control_configured, stt_enabled_models_isolated
):
    """R2 AC1: the curation list visibly distinguishes a downloaded-and-ready
    model from a not-yet-downloaded (pullable) one."""
    with aioresponses_strict() as m:
        _mock_catalog(m)
        resp = authenticated_admin.get("/api/v1/transcribe/models")

    assert resp.status_code == 200, resp.text
    by_id = {e["id"]: e for e in resp.json()["data"]}

    # whisper-small is held by the backend -> downloaded-and-ready.
    assert by_id["whisper-small"]["downloaded"] is True
    assert by_id["whisper-small"]["availability"] == "downloaded"
    # whisper-large-v3 is catalog-only -> pullable, visibly distinct.
    assert by_id["whisper-large-v3"]["downloaded"] is False
    assert by_id["whisper-large-v3"]["availability"] == "pullable"


@pytest.mark.tier1
def test_curation_list_distinction_is_legible_without_a_second_call(
    authenticated_admin, stt_control_configured, stt_enabled_models_isolated
):
    """R2 AC2: the distinction rides on the same curation response as the enabled
    data — every entry carries downloaded + availability + enabled together, so
    no separate management view/round-trip is needed to tell them apart."""
    with aioresponses_strict() as m:
        _mock_catalog(m)
        resp = authenticated_admin.get("/api/v1/transcribe/models")

    assert resp.status_code == 200, resp.text
    entries = resp.json()["data"]
    assert entries, "expected a non-empty curation list"
    for entry in entries:
        assert {"downloaded", "availability", "enabled"} <= set(entry)
        # downloaded and availability never disagree.
        if entry["downloaded"]:
            assert entry["availability"] == "downloaded"
        else:
            assert entry["availability"] == "pullable"


@pytest.mark.tier1
def test_curation_distinction_survives_enabled_overlay(
    authenticated_admin, stt_control_configured, stt_enabled_models_isolated
):
    """Enabling a pullable model does not flip its download distinction: the
    curation list still shows it as pullable, just enabled."""
    resp = authenticated_admin.post(
        "/api/v1/transcribe/models/whisper-large-v3/enabled", json={"enabled": True}
    )
    assert resp.status_code == 200, resp.text

    with aioresponses_strict() as m:
        _mock_catalog(m)
        listing = authenticated_admin.get("/api/v1/transcribe/models")

    entry = {e["id"]: e for e in listing.json()["data"]}["whisper-large-v3"]
    assert entry["enabled"] is True
    assert entry["downloaded"] is False
    assert entry["availability"] == "pullable"


# ---------------------------------------------------------------------------
# End-user model selection (cavekit-audio-transcribe-picker R4, net new)
# ---------------------------------------------------------------------------
#
# GET /models/selectable — the enabled-only list a plain user may choose from.
# POST /models/selected  — save a chosen model as the user's personal setting.


@pytest.mark.tier0
def test_selectable_requires_authentication(client, stt_control_configured):
    """Even the read-only selectable list requires an authenticated user."""
    resp = client.get("/api/v1/transcribe/models/selectable")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_select_requires_authentication(client):
    """Saving a selection requires an authenticated user."""
    resp = client.post(
        "/api/v1/transcribe/models/selected", json={"model_id": "whisper-small"}
    )
    assert resp.status_code in (401, 403)


@pytest.mark.tier1
def test_selectable_lists_only_admin_enabled_models(
    authenticated_user, stt_control_configured, stt_enabled_models_isolated, test_app
):
    """AC1+AC2: a plain user's selectable list contains ONLY admin-enabled models.

    Two models exist in the catalog; the admin has enabled only one. The end-user
    selectable endpoint returns just that one — the disabled model is absent.
    """
    test_app.state.config.STT_ENABLED_MODELS = {"whisper-small": True}
    with aioresponses_strict() as m:
        _mock_catalog(m)
        resp = authenticated_user.get("/api/v1/transcribe/models/selectable")

    assert resp.status_code == 200, resp.text
    by_id = {e["id"]: e for e in resp.json()["data"]}
    assert set(by_id) == {"whisper-small"}
    assert by_id["whisper-small"]["enabled"] is True


@pytest.mark.tier1
def test_selectable_excludes_admin_disabled_model(
    authenticated_user, stt_control_configured, stt_enabled_models_isolated, test_app
):
    """AC3: a model the admin has disabled is not in the user's selectable list."""
    test_app.state.config.STT_ENABLED_MODELS = {"whisper-small": False}
    with aioresponses_strict() as m:
        _mock_catalog(m)
        resp = authenticated_user.get("/api/v1/transcribe/models/selectable")

    assert resp.status_code == 200, resp.text
    ids = {e["id"] for e in resp.json()["data"]}
    assert "whisper-small" not in ids
    assert "whisper-large-v3" not in ids  # never enabled either


@pytest.mark.tier1
def test_selectable_empty_when_none_enabled(
    authenticated_user, stt_control_configured, stt_enabled_models_isolated
):
    """No admin has enabled anything, but a model is downloaded and serving.

    Per the R5 graceful empty state (T-032), this is the *fallback* mode: the
    selectable ``data`` list is empty, but the picker falls back to the router's
    active model rather than showing a broken control. (``_mock_catalog`` has
    whisper-small ``loaded``.)
    """
    with aioresponses_strict() as m:
        _mock_catalog(m)
        resp = authenticated_user.get("/api/v1/transcribe/models/selectable")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"] == []
    assert body["mode"] == "fallback"
    assert body["active_model"] == "whisper-small"


@pytest.mark.tier1
def test_select_enabled_model_persists_as_user_setting(
    authenticated_user, stt_enabled_models_isolated, test_app
):
    """AC1: a plain user saves an admin-enabled model as their personal choice.

    The save endpoint does not require the STT backend (it validates against the
    admin enabled map, config-only), and the chosen model is persisted under the
    user's per-user settings (ui.audio.stt.model), retrievable on reload.
    """
    test_app.state.config.STT_ENABLED_MODELS = {"whisper-small": True}

    resp = authenticated_user.post(
        "/api/v1/transcribe/models/selected", json={"model_id": "whisper-small"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"model_id": "whisper-small"}

    # The choice is persisted as a per-user setting (a "reload" reads it back).
    settings = authenticated_user.get("/api/v1/users/user/settings").json()
    assert settings["ui"]["audio"]["stt"]["model"] == "whisper-small"


@pytest.mark.tier1
def test_select_disabled_model_rejected(
    authenticated_user, stt_enabled_models_isolated, test_app
):
    """AC3: selecting a model the admin has disabled is rejected (403)."""
    test_app.state.config.STT_ENABLED_MODELS = {"whisper-small": False}

    resp = authenticated_user.post(
        "/api/v1/transcribe/models/selected", json={"model_id": "whisper-small"}
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.tier1
def test_select_unenabled_model_rejected(
    authenticated_user, stt_enabled_models_isolated, test_app
):
    """AC3: selecting a model the admin never enabled (absent from map) is rejected."""
    test_app.state.config.STT_ENABLED_MODELS = {}

    resp = authenticated_user.post(
        "/api/v1/transcribe/models/selected", json={"model_id": "whisper-large-v3"}
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.tier1
def test_select_rejected_choice_not_persisted(
    authenticated_user, stt_enabled_models_isolated, test_app
):
    """A rejected selection must not leak into the user's persisted settings."""
    test_app.state.config.STT_ENABLED_MODELS = {"whisper-small": False}

    authenticated_user.post(
        "/api/v1/transcribe/models/selected", json={"model_id": "whisper-small"}
    )

    settings = authenticated_user.get("/api/v1/users/user/settings").json()
    stt = ((settings or {}).get("ui") or {}).get("audio", {}).get("stt", {})
    assert stt.get("model") is None


# ---------------------------------------------------------------------------
# R5: Model deletion under the active-model invariant (DELETE /models/{id})
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_transcribe_delete_requires_admin(authenticated_user, stt_control_configured):
    """Deletion is an admin management mutation — a plain user is rejected."""
    resp = authenticated_user.delete("/api/v1/transcribe/models/whisper-small")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_transcribe_delete_unauthenticated_rejected(client, stt_control_configured):
    resp = client.delete("/api/v1/transcribe/models/whisper-small")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_transcribe_delete_unconfigured_backend(authenticated_admin, test_app):
    """No control URL configured -> 400 (client/config condition)."""
    original = getattr(test_app.state.config, "STT_CONTROL_BASE_URL", "")
    test_app.state.config.STT_CONTROL_BASE_URL = ""
    try:
        resp = authenticated_admin.delete("/api/v1/transcribe/models/whisper-small")
        assert resp.status_code == 400
    finally:
        test_app.state.config.STT_CONTROL_BASE_URL = original


@pytest.mark.tier1
def test_transcribe_delete_non_active_model_succeeds(authenticated_admin, stt_control_configured):
    """R5 AC1: a downloaded model that is not the active one is deleted via a
    single control-port delete command, reclaiming its storage."""
    with aioresponses_strict() as m:
        # whisper-small is loaded (active); whisper-large-v3 is downloaded but
        # unloaded -> deletable.
        m.get(
            f"{CONTROL_URL}/api/models",
            status=200,
            payload={
                "data": [
                    {"id": "whisper-small", "status": {"value": "loaded"}},
                    {"id": "whisper-large-v3", "status": {"value": "unloaded"}},
                ]
            },
        )
        m.get(
            f"{CONTROL_URL}/api/models/available",
            status=200,
            payload=[{"id": "whisper-small"}, {"id": "whisper-large-v3"}],
        )
        m.post(f"{CONTROL_URL}/api/models/delete", status=200, payload={"ok": True})
        resp = authenticated_admin.delete("/api/v1/transcribe/models/whisper-large-v3")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == "whisper-large-v3"
    assert body["status"] == "deleted"


@pytest.mark.tier1
def test_transcribe_delete_active_model_rejected(authenticated_admin, stt_control_configured):
    """R5 AC4 / R7 AC2: deleting the currently-active model is refused with 409,
    and no delete command ever reaches the backend."""
    with aioresponses_strict() as m:
        m.get(
            f"{CONTROL_URL}/api/models",
            status=200,
            payload={"data": [{"id": "whisper-small", "status": {"value": "loaded"}}]},
        )
        m.get(
            f"{CONTROL_URL}/api/models/available",
            status=200,
            payload=[{"id": "whisper-small"}],
        )
        # No m.post for /api/models/delete: a refused deletion must never reach
        # the backend (aioresponses_strict would error on an unexpected request).
        resp = authenticated_admin.delete("/api/v1/transcribe/models/whisper-small")

    assert resp.status_code == 409, resp.text


@pytest.mark.tier1
def test_transcribe_delete_does_not_corrupt_other_models(authenticated_admin, stt_control_configured):
    """R5 AC3: deleting one model leaves the other downloaded models intact — a
    subsequent listing still reports the surviving model correctly."""
    with aioresponses_strict() as m:
        m.get(
            f"{CONTROL_URL}/api/models",
            status=200,
            payload={
                "data": [
                    {"id": "whisper-small", "status": {"value": "loaded"}},
                    {"id": "whisper-large-v3", "status": {"value": "unloaded"}},
                ]
            },
        )
        m.get(
            f"{CONTROL_URL}/api/models/available",
            status=200,
            payload=[{"id": "whisper-small"}, {"id": "whisper-large-v3"}],
        )
        m.post(f"{CONTROL_URL}/api/models/delete", status=200, payload={"ok": True})
        del_resp = authenticated_admin.delete("/api/v1/transcribe/models/whisper-large-v3")
    assert del_resp.status_code == 200, del_resp.text

    # Backend now holds only whisper-small; a fresh listing still reports it
    # downloaded-and-ready — the surviving model is uncorrupted (R5 AC3), and the
    # deleted one is gone from the ready set (R5 AC2).
    with aioresponses_strict() as m:
        m.get(
            f"{CONTROL_URL}/api/models",
            status=200,
            payload={"data": [{"id": "whisper-small", "status": {"value": "loaded"}}]},
        )
        m.get(
            f"{CONTROL_URL}/api/models/available",
            status=200,
            payload=[{"id": "whisper-small"}, {"id": "whisper-large-v3"}],
        )
        listing = authenticated_admin.get("/api/v1/transcribe/models")

    by_id = {e["id"]: e for e in listing.json()["data"]}
    assert by_id["whisper-small"]["downloaded"] is True
    assert by_id["whisper-small"]["availability"] == "downloaded"
    # R5 AC2: the deleted model is no longer downloaded-and-ready, but remains
    # listed as pullable because it is a known catalog entry.
    assert by_id["whisper-large-v3"]["downloaded"] is False
    assert by_id["whisper-large-v3"]["availability"] == "pullable"


@pytest.mark.tier1
def test_transcribe_delete_backend_unreachable(authenticated_admin, stt_control_configured):
    """Listing views unreachable -> 502; the delete never silently no-ops."""
    with aioresponses_strict() as m:
        m.get(f"{CONTROL_URL}/api/models", status=503, payload={"error": "down"})
        m.get(f"{CONTROL_URL}/api/models/available", status=503, payload={"error": "down"})
        resp = authenticated_admin.delete("/api/v1/transcribe/models/whisper-small")

    assert resp.status_code == 502, resp.text


@pytest.mark.tier1
def test_transcribe_delete_command_failure_surfaces(authenticated_admin, stt_control_configured):
    """A valid non-active target whose backend delete command fails surfaces as 502."""
    with aioresponses_strict() as m:
        m.get(
            f"{CONTROL_URL}/api/models",
            status=200,
            payload={
                "data": [
                    {"id": "whisper-small", "status": {"value": "loaded"}},
                    {"id": "whisper-large-v3", "status": {"value": "unloaded"}},
                ]
            },
        )
        m.get(
            f"{CONTROL_URL}/api/models/available",
            status=200,
            payload=[{"id": "whisper-small"}, {"id": "whisper-large-v3"}],
        )
        m.post(f"{CONTROL_URL}/api/models/delete", status=500, payload={"error": "boom"})
        resp = authenticated_admin.delete("/api/v1/transcribe/models/whisper-large-v3")

    assert resp.status_code == 502, resp.text
# ---------------------------------------------------------------------------
# Active-model indicator in the curation list
# (cavekit-audio-transcribe-picker R3 — T-030)
#
# R3 asks that whichever model the router currently has resident be clearly
# marked in the curation list, distinct from merely "downloaded", and that the
# marker move to the new active model on reload after the active model changes.
# The admin curation endpoint (GET /models) is that list; these tests assert an
# ``active`` flag rides on that very response alongside downloaded/enabled.
# ---------------------------------------------------------------------------


def _mock_two_downloaded(m, loaded_id):
    """Two downloaded models, exactly one reported loaded (``loaded_id``)."""
    m.get(
        f"{CONTROL_URL}/api/models",
        status=200,
        payload={
            "data": [
                {
                    "id": "whisper-small",
                    "status": {"value": "loaded" if loaded_id == "whisper-small" else "unloaded"},
                },
                {
                    "id": "whisper-medium",
                    "status": {"value": "loaded" if loaded_id == "whisper-medium" else "unloaded"},
                },
            ]
        },
    )
    m.get(
        f"{CONTROL_URL}/api/models/available",
        status=200,
        payload=[{"id": "whisper-small"}, {"id": "whisper-medium"}, {"id": "whisper-large-v3"}],
    )


@pytest.mark.tier1
def test_curation_list_marks_active_model(
    authenticated_admin, stt_control_configured, stt_enabled_models_isolated
):
    """R3 AC1: the curation list marks the router's resident model ``active``."""
    with aioresponses_strict() as m:
        _mock_catalog(m)  # whisper-small is loaded; whisper-large-v3 is pullable
        resp = authenticated_admin.get("/api/v1/transcribe/models")

    assert resp.status_code == 200, resp.text
    by_id = {e["id"]: e for e in resp.json()["data"]}
    assert by_id["whisper-small"]["active"] is True
    assert by_id["whisper-large-v3"]["active"] is False


@pytest.mark.tier1
def test_curation_active_marker_distinct_from_downloaded(
    authenticated_admin, stt_control_configured, stt_enabled_models_isolated
):
    """R3 AC2: ``active`` is distinct from ``downloaded`` — a downloaded but not
    loaded model is downloaded yet not active, and the pullable one is neither."""
    with aioresponses_strict() as m:
        _mock_two_downloaded(m, loaded_id="whisper-small")
        resp = authenticated_admin.get("/api/v1/transcribe/models")

    assert resp.status_code == 200, resp.text
    by_id = {e["id"]: e for e in resp.json()["data"]}

    # Resident model: downloaded AND active.
    assert by_id["whisper-small"]["downloaded"] is True
    assert by_id["whisper-small"]["active"] is True
    # Other downloaded model: downloaded but NOT active — marker says more than
    # the downloaded flag alone.
    assert by_id["whisper-medium"]["downloaded"] is True
    assert by_id["whisper-medium"]["active"] is False
    # Pullable model: neither downloaded nor active.
    assert by_id["whisper-large-v3"]["downloaded"] is False
    assert by_id["whisper-large-v3"]["active"] is False


@pytest.mark.tier1
def test_curation_active_marker_present_on_every_entry(
    authenticated_admin, stt_control_configured, stt_enabled_models_isolated
):
    """R3 AC2 (legibility): every entry carries an ``active`` flag on the same
    curation response as downloaded/enabled, so the active model is recognizable
    without a separate management view."""
    with aioresponses_strict() as m:
        _mock_two_downloaded(m, loaded_id="whisper-small")
        resp = authenticated_admin.get("/api/v1/transcribe/models")

    assert resp.status_code == 200, resp.text
    entries = resp.json()["data"]
    assert entries
    for entry in entries:
        assert {"downloaded", "availability", "enabled", "active"} <= set(entry)
    # Exactly one active model across the list.
    assert sum(1 for e in entries if e["active"]) == 1


@pytest.mark.tier1
def test_curation_active_marker_moves_on_reload_after_swap(
    authenticated_admin, stt_control_configured, stt_enabled_models_isolated
):
    """R3 AC3: after the active model changes, a fresh listing moves the marker
    to the newly active model."""
    # Before: whisper-small is resident.
    with aioresponses_strict() as m:
        _mock_two_downloaded(m, loaded_id="whisper-small")
        before = authenticated_admin.get("/api/v1/transcribe/models")
    before_by_id = {e["id"]: e for e in before.json()["data"]}
    assert before_by_id["whisper-small"]["active"] is True
    assert before_by_id["whisper-medium"]["active"] is False

    # After a swap the backend now reports whisper-medium resident; a reload of
    # the curation list moves the marker.
    with aioresponses_strict() as m:
        _mock_two_downloaded(m, loaded_id="whisper-medium")
        after = authenticated_admin.get("/api/v1/transcribe/models")
    after_by_id = {e["id"]: e for e in after.json()["data"]}
    assert after_by_id["whisper-small"]["active"] is False
    assert after_by_id["whisper-medium"]["active"] is True


@pytest.mark.tier1
def test_curation_active_marker_survives_search_and_enabled_overlay(
    authenticated_admin, stt_control_configured, stt_enabled_models_isolated
):
    """The active flag is resolved over the full listing before search narrows it,
    and rides through the enabled overlay: a filtered-in active model stays
    marked active with its enabled state intact."""
    # Enable the active model so both overlays are exercised together.
    resp = authenticated_admin.post(
        "/api/v1/transcribe/models/whisper-small/enabled", json={"enabled": True}
    )
    assert resp.status_code == 200, resp.text

    with aioresponses_strict() as m:
        _mock_catalog(m)  # whisper-small loaded
        resp = authenticated_admin.get(
            "/api/v1/transcribe/models", params={"search": "small"}
        )

    assert resp.status_code == 200, resp.text
    by_id = {e["id"]: e for e in resp.json()["data"]}
    assert set(by_id) == {"whisper-small"}
    assert by_id["whisper-small"]["active"] is True
    assert by_id["whisper-small"]["enabled"] is True


@pytest.mark.tier1
def test_curation_no_active_when_none_loaded(
    authenticated_admin, stt_control_configured, stt_enabled_models_isolated
):
    """When the backend reports no model loaded, no entry is marked active."""
    with aioresponses_strict() as m:
        m.get(
            f"{CONTROL_URL}/api/models",
            status=200,
            payload={"data": [{"id": "whisper-small", "status": {"value": "unloaded"}}]},
        )
        m.get(
            f"{CONTROL_URL}/api/models/available",
            status=200,
            payload=[{"id": "whisper-small"}, {"id": "whisper-large-v3"}],
        )
        resp = authenticated_admin.get("/api/v1/transcribe/models")

    assert resp.status_code == 200, resp.text
    assert all(e["active"] is False for e in resp.json()["data"])

# ---------------------------------------------------------------------------
# Graceful empty/fallback state (cavekit-audio-transcribe-picker R5 — T-032)
#
# GET /models/selectable returns a self-describing envelope with a ``mode`` so
# the picker never renders a broken, empty control:
#   * "enabled"     — admin-enabled models present in ``data``.
#   * "fallback"    — none enabled but a model is actively serving; the picker
#                     falls back to the router's active model (AC1).
#   * "unavailable" — zero models downloaded (the router's permitted no-active
#                     state); a clear message, no control (AC2).
# ---------------------------------------------------------------------------


def _mock_empty_backend(m):
    """Both control views empty: a fresh backend with no models at all."""
    m.get(f"{CONTROL_URL}/api/models", status=200, payload={"data": []})
    m.get(f"{CONTROL_URL}/api/models/available", status=200, payload=[])


@pytest.mark.tier1
def test_selectable_fallback_when_downloaded_but_none_enabled(
    authenticated_user, stt_control_configured, stt_enabled_models_isolated
):
    """R5 AC1: a model is downloaded and serving but none is admin-enabled ->
    the picker falls back to the router's active model, not an empty control."""
    with aioresponses_strict() as m:
        _mock_catalog(m)  # whisper-small is loaded (active); nothing enabled
        resp = authenticated_user.get("/api/v1/transcribe/models/selectable")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"] == []
    assert body["mode"] == "fallback"
    assert body["active_model"] == "whisper-small"
    # The full fallback entry is provided so the picker can name it.
    assert body["fallback_model"]["id"] == "whisper-small"
    assert body["fallback_model"]["downloaded"] is True
    assert body["message"] is None


@pytest.mark.tier1
def test_selectable_unavailable_when_zero_downloaded(
    authenticated_user, stt_control_configured, stt_enabled_models_isolated
):
    """R5 AC2: zero models downloaded (the router's permitted no-active state) ->
    a clear unavailable message, no active model, no misleading control."""
    with aioresponses_strict() as m:
        _mock_empty_backend(m)
        resp = authenticated_user.get("/api/v1/transcribe/models/selectable")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"] == []
    assert body["mode"] == "unavailable"
    assert body["active_model"] is None
    assert body["fallback_model"] is None
    assert isinstance(body["message"], str) and body["message"].strip()


@pytest.mark.tier1
def test_selectable_enabled_mode_when_a_model_is_enabled(
    authenticated_user, stt_control_configured, stt_enabled_models_isolated, test_app
):
    """R5 AC3 (normal path): with a model enabled the picker is in ``enabled``
    mode, offering that model for explicit selection."""
    test_app.state.config.STT_ENABLED_MODELS = {"whisper-small": True}
    with aioresponses_strict() as m:
        _mock_catalog(m)
        resp = authenticated_user.get("/api/v1/transcribe/models/selectable")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "enabled"
    assert {e["id"] for e in body["data"]} == {"whisper-small"}
    assert body["message"] is None


@pytest.mark.tier1
def test_selectable_enabling_transitions_fallback_to_enabled(
    authenticated_user, authenticated_admin, stt_control_configured,
    stt_enabled_models_isolated,
):
    """R5 AC3: enabling at least one model transitions the picker from the
    fallback state to normal enabled-only selection."""
    # Before: nothing enabled, a model serving -> fallback.
    with aioresponses_strict() as m:
        _mock_catalog(m)
        before = authenticated_user.get("/api/v1/transcribe/models/selectable")
    assert before.json()["mode"] == "fallback"

    # Admin enables the downloaded model.
    toggle = authenticated_admin.post(
        "/api/v1/transcribe/models/whisper-small/enabled", json={"enabled": True}
    )
    assert toggle.status_code == 200, toggle.text

    # After: the same picker is now in normal enabled-only selection.
    with aioresponses_strict() as m:
        _mock_catalog(m)
        after = authenticated_user.get("/api/v1/transcribe/models/selectable")
    body = after.json()
    assert body["mode"] == "enabled"
    assert {e["id"] for e in body["data"]} == {"whisper-small"}


# ---------------------------------------------------------------------------
# R6: Auth-ready credential slot on the mutating operations (pull, cancel, delete)
# (cavekit-audio-transcribe-router R6 — T-027)
#
# Each of the three mutating operations defines an OPTIONAL credential slot in
# its request body — accepted but unused/unchecked today, and never forwarded to
# the backend control command (R6 AC3). Supplying it must not change behavior,
# and omitting it must leave every existing caller working exactly as before
# (R6 AC1 — the T-021/T-023/T-026 no-regression guarantee). These tests prove
# both halves against pull, cancel, and delete individually, and confirm the
# credential never reaches the backend request body.
# ---------------------------------------------------------------------------


def _backend_bodies(m, path_suffix):
    """Return the parsed request bodies the router sent to a backend path.

    Inspects aioresponses' recorded requests so a test can assert exactly what
    payload the router forwarded upstream — the pull sends a JSON *string* via
    ``data=`` while cancel/delete send ``json=`` dicts, so both shapes are
    normalized to a dict here.
    """
    bodies = []
    for (method, url), calls in m.requests.items():
        if not str(url).endswith(path_suffix):
            continue
        for call in calls:
            if call.kwargs.get("json") is not None:
                bodies.append(call.kwargs["json"])
            elif call.kwargs.get("data") is not None:
                raw = call.kwargs["data"]
                bodies.append(json.loads(raw) if isinstance(raw, (str, bytes)) else raw)
    return bodies


@pytest.mark.tier1
def test_pull_form_credential_slot_is_optional_and_inert():
    """R6 AC3 (contract): the mutating request body defines an OPTIONAL credential
    slot, defaulting absent, and the router carries the posture in-code (AC4)."""
    from selfai_ui.audio.connections import CREDENTIAL_SLOT, PERIMETER_POSTURE
    from selfai_ui.routers.transcribe import (
        MUTATING_CREDENTIAL_SLOT,
        MUTATING_OPERATION_POSTURE,
        MutatingActionForm,
    )

    # Optional: a request that omits the credential is valid and carries None.
    assert MutatingActionForm().credential is None
    # Present when supplied — the slot a future auth gate reads without reshaping.
    assert MutatingActionForm(credential="tok").credential == "tok"
    # The wire key matches the connections-domain contract (one key, both surfaces).
    assert MUTATING_CREDENTIAL_SLOT == CREDENTIAL_SLOT == "credential"
    # R6 AC4: the network-perimeter posture is stated plainly and carried in-code.
    assert MUTATING_OPERATION_POSTURE == PERIMETER_POSTURE
    assert "network-perimeter" in PERIMETER_POSTURE.lower()


# --- Pull (R6 over R2) ------------------------------------------------------


def _mock_pull_success(m, model_id):
    """Mock a control-port pull stream that ends in a terminal success line."""
    m.post(
        f"{CONTROL_URL}/api/models/pull",
        status=200,
        body=json.dumps({"status": "success", "id": model_id}) + "\n",
    )


@pytest.mark.tier1
def test_pull_accepts_but_ignores_supplied_credential(
    authenticated_admin, stt_control_configured
):
    """R6 AC3: a credential MAY be supplied to the pull; it is accepted, the pull
    still succeeds, and the credential is never forwarded to the backend."""
    with aioresponses_strict() as m:
        _mock_pull_success(m, "whisper-small")
        resp = authenticated_admin.post(
            "/api/v1/transcribe/models/whisper-small/pull",
            json={"credential": "secret-token"},
        )
        bodies = _backend_bodies(m, "/api/models/pull")

    assert resp.status_code == 200, resp.text
    assert "success" in resp.text
    # The backend pull command carried only id/name — never the credential.
    assert bodies, "expected the router to hit the backend pull endpoint"
    for body in bodies:
        assert "credential" not in body
        assert body == {"id": "whisper-small", "name": "whisper-small"}


@pytest.mark.tier1
def test_pull_without_credential_unchanged(
    authenticated_admin, stt_control_configured
):
    """R6 AC1 (no regression): omitting the body pulls exactly as the T-021 path
    always did — same backend command, same streamed success."""
    with aioresponses_strict() as m:
        _mock_pull_success(m, "whisper-small")
        resp = authenticated_admin.post(
            "/api/v1/transcribe/models/whisper-small/pull"
        )
        bodies = _backend_bodies(m, "/api/models/pull")

    assert resp.status_code == 200, resp.text
    assert "success" in resp.text
    assert bodies == [{"id": "whisper-small", "name": "whisper-small"}]


# --- Cancel (R6 over R3) ----------------------------------------------------


@pytest.mark.tier1
def test_cancel_accepts_but_ignores_supplied_credential(
    authenticated_admin, stt_control_configured
):
    """R6 AC3: a credential MAY be supplied to the cancel; accepted, ignored, and
    never forwarded to the backend cancel command."""
    with aioresponses_strict() as m:
        m.post(f"{CONTROL_URL}/api/models/pull/cancel", status=200, payload={"ok": True})
        resp = authenticated_admin.post(
            "/api/v1/transcribe/models/whisper-small/cancel",
            json={"credential": "secret-token"},
        )
        bodies = _backend_bodies(m, "/api/models/pull/cancel")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": "whisper-small", "status": "cancelled"}
    assert bodies, "expected the router to hit the backend cancel endpoint"
    for body in bodies:
        assert "credential" not in body
        assert body == {"id": "whisper-small", "name": "whisper-small"}


@pytest.mark.tier1
def test_cancel_without_credential_unchanged(
    authenticated_admin, stt_control_configured
):
    """R6 AC1 (no regression): omitting the body cancels exactly as the T-023 path
    always did."""
    with aioresponses_strict() as m:
        m.post(f"{CONTROL_URL}/api/models/pull/cancel", status=200, payload={"ok": True})
        resp = authenticated_admin.post(
            "/api/v1/transcribe/models/whisper-small/cancel"
        )
        bodies = _backend_bodies(m, "/api/models/pull/cancel")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": "whisper-small", "status": "cancelled"}
    assert bodies == [{"id": "whisper-small", "name": "whisper-small"}]


# --- Delete (R6 over R5) ----------------------------------------------------


def _mock_deletable(m):
    """whisper-large-v3 is downloaded but unloaded -> a deletable non-active model."""
    m.get(
        f"{CONTROL_URL}/api/models",
        status=200,
        payload={
            "data": [
                {"id": "whisper-small", "status": {"value": "loaded"}},
                {"id": "whisper-large-v3", "status": {"value": "unloaded"}},
            ]
        },
    )
    m.get(
        f"{CONTROL_URL}/api/models/available",
        status=200,
        payload=[{"id": "whisper-small"}, {"id": "whisper-large-v3"}],
    )


@pytest.mark.tier1
def test_delete_accepts_but_ignores_supplied_credential(
    authenticated_admin, stt_control_configured
):
    """R6 AC3: a credential MAY be supplied to the delete; accepted, ignored, the
    delete still succeeds, and the credential never reaches the backend."""
    with aioresponses_strict() as m:
        _mock_deletable(m)
        m.post(f"{CONTROL_URL}/api/models/delete", status=200, payload={"ok": True})
        resp = authenticated_admin.request(
            "DELETE",
            "/api/v1/transcribe/models/whisper-large-v3",
            json={"credential": "secret-token"},
        )
        bodies = _backend_bodies(m, "/api/models/delete")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": "whisper-large-v3", "status": "deleted"}
    assert bodies, "expected the router to hit the backend delete endpoint"
    for body in bodies:
        assert "credential" not in body
        assert body == {"id": "whisper-large-v3", "name": "whisper-large-v3"}


@pytest.mark.tier1
def test_delete_without_credential_unchanged(
    authenticated_admin, stt_control_configured
):
    """R6 AC1 (no regression): omitting the body deletes exactly as the T-026 path
    always did — same backend command, credential absent."""
    with aioresponses_strict() as m:
        _mock_deletable(m)
        m.post(f"{CONTROL_URL}/api/models/delete", status=200, payload={"ok": True})
        resp = authenticated_admin.delete(
            "/api/v1/transcribe/models/whisper-large-v3"
        )
        bodies = _backend_bodies(m, "/api/models/delete")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": "whisper-large-v3", "status": "deleted"}
    assert bodies == [{"id": "whisper-large-v3", "name": "whisper-large-v3"}]


@pytest.mark.tier1
def test_delete_active_model_still_refused_with_credential(
    authenticated_admin, stt_control_configured
):
    """R6 does not weaken R5/R7: supplying a credential does not bypass the
    active-model gate — deleting the active model is still refused (409), and no
    delete command reaches the backend."""
    with aioresponses_strict() as m:
        m.get(
            f"{CONTROL_URL}/api/models",
            status=200,
            payload={"data": [{"id": "whisper-small", "status": {"value": "loaded"}}]},
        )
        m.get(
            f"{CONTROL_URL}/api/models/available",
            status=200,
            payload=[{"id": "whisper-small"}],
        )
        # No delete mock: a refused deletion must never reach the backend.
        resp = authenticated_admin.request(
            "DELETE",
            "/api/v1/transcribe/models/whisper-small",
            json={"credential": "secret-token"},
        )

    assert resp.status_code == 409, resp.text
