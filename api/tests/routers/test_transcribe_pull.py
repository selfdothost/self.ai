"""Transcribe router pull-endpoint tests (cavekit-audio-transcribe-router R2).

The pull endpoint proxies the self-hosted STT backend's control-port
``/api/models/pull`` NDJSON stream over aiohttp; upstream is mocked with
aioresponses. Covers auth gating, the unconfigured-backend guard, incremental
progress, terminal success vs error distinction, and the invariant that a failed
pull never leaves a partial model presented as downloaded-and-ready.
"""

import asyncio
import json

import aiohttp
import pytest

from tests.mocks.external_services import aioresponses_strict

CONTROL_URL = "http://self-transcribe:9000"
MODEL_ID = "whisper-large-v3"
PULL_URL = f"{CONTROL_URL}/api/models/pull"
CANCEL_URL = f"{CONTROL_URL}/api/models/pull/cancel"


@pytest.fixture
def stt_control_configured(test_app):
    original = getattr(test_app.state.config, "STT_CONTROL_BASE_URL", "")
    test_app.state.config.STT_CONTROL_BASE_URL = CONTROL_URL
    try:
        yield
    finally:
        test_app.state.config.STT_CONTROL_BASE_URL = original


def _events(resp):
    """Parse an application/x-ndjson pull response into a list of event dicts."""
    return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


@pytest.mark.tier0
def test_pull_requires_admin(authenticated_user, stt_control_configured):
    """Pull is a mutating admin management action — a plain user is rejected."""
    resp = authenticated_user.post(f"/api/v1/transcribe/models/{MODEL_ID}/pull")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_pull_unauthenticated_rejected(client, stt_control_configured):
    resp = client.post(f"/api/v1/transcribe/models/{MODEL_ID}/pull")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_pull_unconfigured_backend(authenticated_admin, test_app):
    """No control URL configured -> 400 before any streaming begins."""
    original = getattr(test_app.state.config, "STT_CONTROL_BASE_URL", "")
    test_app.state.config.STT_CONTROL_BASE_URL = ""
    try:
        resp = authenticated_admin.post(f"/api/v1/transcribe/models/{MODEL_ID}/pull")
        assert resp.status_code == 400
    finally:
        test_app.state.config.STT_CONTROL_BASE_URL = original


@pytest.mark.tier1
def test_pull_streams_incremental_progress_then_success(authenticated_admin, stt_control_configured):
    """AC1+AC2+AC3: model pulls with no redeploy, progress is incremental, and
    the stream terminates in a single `success` event."""
    body = (
        json.dumps({"status": "downloading", "completed": 250, "total": 1000})
        + "\n"
        + json.dumps({"status": "downloading", "completed": 750, "total": 1000})
        + "\n"
        + json.dumps({"status": "success"})
        + "\n"
    )
    with aioresponses_strict() as m:
        m.post(PULL_URL, status=200, body=body)
        resp = authenticated_admin.post(f"/api/v1/transcribe/models/{MODEL_ID}/pull")

    assert resp.status_code == 200, resp.text
    events = _events(resp)

    pulling = [e for e in events if e["phase"] == "pulling"]
    terminal = events[-1]
    # More than one non-terminal progress event -> genuinely incremental.
    assert len(pulling) >= 2
    assert pulling[0]["percent"] == 25.0
    assert pulling[1]["percent"] == 75.0
    # Exactly one terminal event, and it is a success.
    assert terminal["phase"] == "success"
    assert sum(1 for e in events if e["phase"] in ("success", "error")) == 1


@pytest.mark.tier1
def test_pull_success_makes_model_appear_downloaded(authenticated_admin, stt_control_configured):
    """AC3: after a successful pull, the model shows in the R1 downloaded set."""
    with aioresponses_strict() as m:
        m.post(PULL_URL, status=200, body=json.dumps({"status": "success"}) + "\n")
        pull_resp = authenticated_admin.post(f"/api/v1/transcribe/models/{MODEL_ID}/pull")
    assert _events(pull_resp)[-1]["phase"] == "success"

    # The backend now holds the model; the listing (T-020) reflects it as ready.
    with aioresponses_strict() as m:
        m.get(f"{CONTROL_URL}/api/models", status=200, payload={"data": [{"id": MODEL_ID}]})
        m.get(f"{CONTROL_URL}/api/models/available", status=200, payload=[{"id": MODEL_ID}])
        list_resp = authenticated_admin.get("/api/v1/transcribe/models")

    by_id = {e["id"]: e for e in list_resp.json()["data"]}
    assert by_id[MODEL_ID]["downloaded"] is True
    assert by_id[MODEL_ID]["availability"] == "downloaded"


@pytest.mark.tier1
def test_pull_failure_reported_distinguishably(authenticated_admin, stt_control_configured):
    """AC4: a failed pull terminates in an `error` event, not a success."""
    body = (
        json.dumps({"status": "downloading", "completed": 100, "total": 1000})
        + "\n"
        + json.dumps({"error": "no space left on device"})
        + "\n"
    )
    with aioresponses_strict() as m:
        m.post(PULL_URL, status=200, body=body)
        resp = authenticated_admin.post(f"/api/v1/transcribe/models/{MODEL_ID}/pull")

    events = _events(resp)
    assert events[-1]["phase"] == "error"
    assert events[-1]["message"] == "no space left on device"
    assert not any(e["phase"] == "success" for e in events)


@pytest.mark.tier1
def test_pull_backend_http_error_yields_terminal_error(authenticated_admin, stt_control_configured):
    """An upstream HTTP failure is surfaced as a single terminal error event."""
    with aioresponses_strict() as m:
        m.post(PULL_URL, status=500, payload={"error": "backend exploded"})
        resp = authenticated_admin.post(f"/api/v1/transcribe/models/{MODEL_ID}/pull")

    events = _events(resp)
    assert len(events) == 1
    assert events[0]["phase"] == "error"


@pytest.mark.tier1
def test_truncated_stream_treated_as_error_not_downloaded(authenticated_admin, stt_control_configured):
    """AC5: a stream that ends without a terminal event becomes an `error`, and
    the model is not reported downloaded-and-ready afterwards."""
    # Progress only, no terminal success/error line.
    body = json.dumps({"status": "downloading", "completed": 100, "total": 1000}) + "\n"
    with aioresponses_strict() as m:
        m.post(PULL_URL, status=200, body=body)
        resp = authenticated_admin.post(f"/api/v1/transcribe/models/{MODEL_ID}/pull")

    events = _events(resp)
    assert events[-1]["phase"] == "error"
    assert not any(e["phase"] == "success" for e in events)

    # The backend never gained the model; the listing does not show it as ready.
    with aioresponses_strict() as m:
        m.get(f"{CONTROL_URL}/api/models", status=200, payload={"data": []})
        m.get(f"{CONTROL_URL}/api/models/available", status=200, payload=[{"id": MODEL_ID}])
        list_resp = authenticated_admin.get("/api/v1/transcribe/models")

    by_id = {e["id"]: e for e in list_resp.json()["data"]}
    assert by_id[MODEL_ID]["downloaded"] is False
    assert by_id[MODEL_ID]["availability"] == "pullable"


# ---------------------------------------------------------------------------
# T-022: fail-closed on a stalled/silent pull (no error line, no success line).
#
# The existing tests above cover an *explicit* backend error line, an upstream
# HTTP >= 400, and a truncated-but-clean stream. Not covered before T-022: a
# pull that neither errors nor succeeds nor cleanly ends — it just goes silent
# until the aiohttp client timeout (AIOHTTP_CLIENT_TIMEOUT) fires, or the
# connection drops mid-flight. Both must fail *closed*: a single terminal
# `error` event, never a `success`, so the model is never presented as
# downloaded-and-ready (R2 AC4/AC5). These exercise _stream_pull's
# `except Exception` arm, which catches the fired timeout (asyncio.TimeoutError
# is an Exception subclass) rather than hanging or leaking a partial as ready.
# ---------------------------------------------------------------------------


@pytest.mark.tier1
def test_pull_client_timeout_yields_terminal_error(authenticated_admin, stt_control_configured):
    """AC4/AC5: a stalled pull whose client timeout fires becomes a terminal error.

    A silent backend (no progress, no success, no error) trips
    AIOHTTP_CLIENT_TIMEOUT; aiohttp raises asyncio.TimeoutError, which
    _stream_pull's `except Exception` turns into one terminal `error` event.
    The pull is never reported as a success, so nothing partial is presented as
    downloaded-and-ready.
    """
    with aioresponses_strict() as m:
        m.post(PULL_URL, exception=asyncio.TimeoutError())
        resp = authenticated_admin.post(f"/api/v1/transcribe/models/{MODEL_ID}/pull")

    events = _events(resp)
    assert len(events) == 1
    assert events[0]["phase"] == "error"
    assert events[0]["id"] == MODEL_ID


# ---------------------------------------------------------------------------
# R3: Pull cancellation (POST /models/{id}/cancel)
# ---------------------------------------------------------------------------


@pytest.mark.tier0
def test_cancel_requires_admin(authenticated_user, stt_control_configured):
    """Cancel is a mutating admin management action — a plain user is rejected."""
    resp = authenticated_user.post(f"/api/v1/transcribe/models/{MODEL_ID}/cancel")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_cancel_unauthenticated_rejected(client, stt_control_configured):
    resp = client.post(f"/api/v1/transcribe/models/{MODEL_ID}/cancel")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_cancel_unconfigured_backend(authenticated_admin, test_app):
    """No control URL configured -> 400 before any control-port call."""
    original = getattr(test_app.state.config, "STT_CONTROL_BASE_URL", "")
    test_app.state.config.STT_CONTROL_BASE_URL = ""
    try:
        resp = authenticated_admin.post(f"/api/v1/transcribe/models/{MODEL_ID}/cancel")
        assert resp.status_code == 400
    finally:
        test_app.state.config.STT_CONTROL_BASE_URL = original


@pytest.mark.tier1
def test_cancel_in_flight_pull_sends_backend_command(authenticated_admin, stt_control_configured):
    """AC1: while a pull is in flight, the cancel endpoint issues a control-port
    cancel command and reports the pull cancelled.

    The in-flight pull stream and the cancel POST are mocked together: the cancel
    hits the backend's ``pull/cancel`` route, and the still-open pull stream
    terminates in a `cancelled` event (the backend's response to being stopped).
    """
    # The pull stream, once the backend is told to cancel, ends with a terminal
    # `cancelled` chunk rather than a `success`.
    pull_body = (
        json.dumps({"status": "downloading", "completed": 100, "total": 1000})
        + "\n"
        + json.dumps({"status": "cancelled"})
        + "\n"
    )
    with aioresponses_strict() as m:
        m.post(PULL_URL, status=200, body=pull_body)
        m.post(CANCEL_URL, status=200, payload={"ok": True})

        pull_resp = authenticated_admin.post(f"/api/v1/transcribe/models/{MODEL_ID}/pull")
        cancel_resp = authenticated_admin.post(f"/api/v1/transcribe/models/{MODEL_ID}/cancel")

    assert cancel_resp.status_code == 200, cancel_resp.text
    assert cancel_resp.json() == {"id": MODEL_ID, "status": "cancelled"}

    # The open pull stream terminated in a `cancelled` event, not a success.
    events = _events(pull_resp)
    assert events[-1]["phase"] == "cancelled"
    assert not any(e["phase"] == "success" for e in events)


@pytest.mark.tier1
def test_pull_midstream_connection_drop_yields_terminal_error(authenticated_admin, stt_control_configured):
    """AC4/AC5: a connection that drops mid-pull surfaces as a terminal error.

    Distinct from a clean truncation (covered above): here the transport itself
    raises. _stream_pull's `except Exception` catches it and emits a single
    terminal `error` event carrying the connection failure, never a success.
    """
    with aioresponses_strict() as m:
        m.post(PULL_URL, exception=aiohttp.ClientConnectionError("connection reset by peer"))
        resp = authenticated_admin.post(f"/api/v1/transcribe/models/{MODEL_ID}/pull")

    events = _events(resp)
    assert len(events) == 1
    assert events[0]["phase"] == "error"
    assert events[0]["id"] == MODEL_ID
    assert "connection error" in (events[0]["message"] or "")
    assert not any(e["phase"] == "success" for e in events)


@pytest.mark.tier1
def test_pull_silent_then_dropped_never_reports_downloaded(authenticated_admin, stt_control_configured):
    """AC5: after a timed-out/dropped pull, the listing still shows the model as
    merely pullable — the failure left no partial presented as ready."""
    with aioresponses_strict() as m:
        m.post(PULL_URL, exception=asyncio.TimeoutError())
        resp = authenticated_admin.post(f"/api/v1/transcribe/models/{MODEL_ID}/pull")
    assert _events(resp)[-1]["phase"] == "error"

    # The backend never gained the model; the R1 listing reflects only what it
    # actually holds, so the model remains pullable, not downloaded-and-ready.
    with aioresponses_strict() as m:
        m.get(f"{CONTROL_URL}/api/models", status=200, payload={"data": []})
        m.get(f"{CONTROL_URL}/api/models/available", status=200, payload=[{"id": MODEL_ID}])
        list_resp = authenticated_admin.get("/api/v1/transcribe/models")

    by_id = {e["id"]: e for e in list_resp.json()["data"]}
    assert by_id[MODEL_ID]["downloaded"] is False
    assert by_id[MODEL_ID]["availability"] == "pullable"


@pytest.mark.tier1
def test_cancel_does_not_leave_model_downloaded(authenticated_admin, stt_control_configured):
    """AC2: after a cancel, the R1 listing does not report the model as
    downloaded-and-ready — the backend never gained it."""
    with aioresponses_strict() as m:
        m.post(CANCEL_URL, status=200, payload={"ok": True})
        cancel_resp = authenticated_admin.post(f"/api/v1/transcribe/models/{MODEL_ID}/cancel")
    assert cancel_resp.status_code == 200, cancel_resp.text

    with aioresponses_strict() as m:
        m.get(f"{CONTROL_URL}/api/models", status=200, payload={"data": []})
        m.get(f"{CONTROL_URL}/api/models/available", status=200, payload=[{"id": MODEL_ID}])
        list_resp = authenticated_admin.get("/api/v1/transcribe/models")

    by_id = {e["id"]: e for e in list_resp.json()["data"]}
    assert by_id[MODEL_ID]["downloaded"] is False
    assert by_id[MODEL_ID]["availability"] == "pullable"


@pytest.mark.tier1
def test_cancel_then_repull_same_model_succeeds(authenticated_admin, stt_control_configured):
    """AC3: cancelling leaves no leftover state that blocks a subsequent NEW pull
    of the same model — a fresh pull after a cancel completes normally."""
    # Cancel an in-flight pull.
    with aioresponses_strict() as m:
        m.post(CANCEL_URL, status=200, payload={"ok": True})
        cancel_resp = authenticated_admin.post(f"/api/v1/transcribe/models/{MODEL_ID}/cancel")
    assert cancel_resp.status_code == 200, cancel_resp.text

    # A brand-new pull of the SAME model now runs to a clean success.
    with aioresponses_strict() as m:
        m.post(PULL_URL, status=200, body=json.dumps({"status": "success"}) + "\n")
        repull_resp = authenticated_admin.post(f"/api/v1/transcribe/models/{MODEL_ID}/pull")

    assert repull_resp.status_code == 200, repull_resp.text
    assert _events(repull_resp)[-1]["phase"] == "success"


@pytest.mark.tier1
def test_cancel_backend_rejection_surfaces_as_502(authenticated_admin, stt_control_configured):
    """An upstream failure of the cancel command surfaces as 502, not a silent
    success."""
    with aioresponses_strict() as m:
        m.post(CANCEL_URL, status=500, payload={"error": "boom"})
        resp = authenticated_admin.post(f"/api/v1/transcribe/models/{MODEL_ID}/cancel")
    assert resp.status_code == 502, resp.text
