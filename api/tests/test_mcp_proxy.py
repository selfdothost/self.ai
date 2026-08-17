"""MCP front-door proxy — relay correctness (self.ai#25).

The proxy is a transparent reverse proxy for MCP Streamable-HTTP: it forwards
POST/GET/DELETE on ``/mcp/{server}`` to that backend's ``/mcp`` and streams the
SSE response back, gated by ``get_verified_user``. These tests pin the relay
contract without standing up a real backend — ``aiohttp.ClientSession.request``
is patched to a fake response, so what's under test is the proxy's header/body/
method handling and its auth gate, not the network.

The route is exercised through the real FastAPI app (``client`` + the
``authenticated_user``/``authenticated_admin`` fixtures), so ``get_verified_user``
and the router mount run for real.
"""

import json

import aiohttp
import pytest

from selfai_ui.routers import mcp_proxy

ECHO_URL = "http://mcp-echo.self-ai.svc:8084/mcp"

INIT_BODY = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "0"},
        },
    }
)

# A realistic initialize SSE response: one `event: message` carrying the
# JSON-RPC result, terminated by a blank line (the Streamable-HTTP framing).
INIT_SSE = (
    'event: message\n'
    'data: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18",'
    '"capabilities":{"tools":{}},"serverInfo":{"name":"mcp-echo","version":"0.1.0"}}}\n\n'
)


class _FakeContent:
    """Mimics aiohttp's `resp.content.iter_any()` — yields the backend's bytes."""

    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_any(self):
        for chunk in self._chunks:
            yield chunk


class _FakeResponse:
    def __init__(self, status, headers, body_chunks):
        self.status = status
        self.headers = headers
        self.content = _FakeContent(body_chunks)


def _patch_backend(monkeypatch, response, captured):
    """Route `ClientSession.request` to a fake response, recording the call."""

    async def fake_request(self, method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = {k.lower(): v for k, v in (kwargs.get("headers") or {}).items()}
        captured["data"] = kwargs.get("data")
        return response

    monkeypatch.setattr(aiohttp.ClientSession, "request", fake_request)


@pytest.fixture
def captured():
    return {}


@pytest.fixture(autouse=True)
def reset_backends():
    """Ensure the seed backend is present regardless of import-order/env leakage."""
    mcp_proxy._BACKENDS["echo"] = ECHO_URL
    yield


def _headers(h):
    return {k.lower(): v for k, v in h.items()}


# --- auth gate ----------------------------------------------------------------


@pytest.mark.tier0
def test_unauthenticated_is_rejected(client, captured, monkeypatch):
    """No credential → 403, and the backend is never reached."""
    _patch_backend(monkeypatch, _FakeResponse(200, {"Content-Type": "text/event-stream"}, [b"x"]), captured)
    resp = client.post("/mcp/echo", data=INIT_BODY)
    assert resp.status_code == 403
    assert captured == {}  # the proxy never forwarded


@pytest.mark.tier0
def test_unknown_server_is_404(authenticated_user, captured, monkeypatch):
    """An authenticated caller asking for a server not in the table gets 404."""
    _patch_backend(monkeypatch, _FakeResponse(200, {"Content-Type": "text/event-stream"}, [b"x"]), captured)
    resp = authenticated_user.post("/mcp/nonexistent", data=INIT_BODY)
    assert resp.status_code == 404
    assert captured == {}


# --- the relay ----------------------------------------------------------------


@pytest.mark.tier0
def test_initialize_is_relayed_with_session_id(authenticated_user, captured, monkeypatch):
    """The load-bearing case: initialize forwards verbatim and the SSE response
    streams back with the backend's Mcp-Session-Id relayed."""
    _patch_backend(
        monkeypatch,
        _FakeResponse(
            200,
            {"Content-Type": "text/event-stream", "mcp-session-id": "sess-abc", "cache-control": "no-cache"},
            [INIT_SSE.encode()],
        ),
        captured,
    )
    resp = authenticated_user.post(
        "/mcp/echo",
        data=INIT_BODY,
        headers={"Accept": "application/json, text/event-stream"},
    )

    assert resp.status_code == 200
    # The backend's session id and content-type are relayed to the caller.
    h = _headers(resp.headers)
    assert h["mcp-session-id"] == "sess-abc"
    # Starlette appends `; charset=utf-8` to text/* media types on
    # StreamingResponse, which is fine for SSE clients — assert the base type.
    assert h["content-type"].startswith("text/event-stream")
    # The SSE body came back verbatim (relayed chunk-by-chunk, not rewritten).
    assert INIT_SSE in resp.text

    # The proxy forwarded to the echo backend's /mcp, with the body intact and
    # the MCP-critical Accept header carried through.
    assert captured["method"] == "POST"
    assert captured["url"] == ECHO_URL
    assert captured["data"] == INIT_BODY.encode()
    assert captured["headers"]["accept"] == "application/json, text/event-stream"


@pytest.mark.tier0
def test_accept_header_is_forwarded_verbatim_not_injected(authenticated_user, captured, monkeypatch):
    """The proxy is transparent: a client that sends only `application/json`
    gets exactly that forwarded — the proxy does NOT inject `text/event-stream`
    to paper over a misbehaving client. (The backend will 406; that is its call,
    not the proxy's to pre-empt.)"""
    _patch_backend(monkeypatch, _FakeResponse(200, {"Content-Type": "text/event-stream"}, [b""]), captured)
    authenticated_user.post("/mcp/echo", data=INIT_BODY, headers={"Accept": "application/json"})
    assert captured["headers"]["accept"] == "application/json"


# --- downstream auth: the caller's self.ai credential stops at the proxy -------


@pytest.mark.tier0
def test_selfai_credential_is_never_forwarded(authenticated_user, captured, monkeypatch):
    """The load-bearing security property. The caller authenticates to self.ai
    with `Authorization` (and/or a session cookie); a backend must never see
    either. The first cut of this proxy forwarded everything not hop-by-hop,
    which handed every registered backend a working self.ai credential."""
    _patch_backend(monkeypatch, _FakeResponse(200, {"Content-Type": "text/event-stream"}, [b""]), captured)
    authenticated_user.post(
        "/mcp/echo",
        data=INIT_BODY,
        headers={"Accept": "application/json, text/event-stream"},
        cookies={"token": "a-session-jwt"},
    )
    fwd = captured["headers"]
    assert "authorization" not in fwd, "the self.ai credential leaked to the backend"
    assert "cookie" not in fwd, "the self.ai session cookie leaked to the backend"


@pytest.mark.tier0
def test_backend_credential_is_renamed_to_authorization(authenticated_user, captured, monkeypatch):
    """A caller needing to authenticate to the BACKEND sends its credential as
    X-Mcp-Authorization; the proxy renames it to Authorization on the way out.
    This is how glab-mcp's REMOTE_AUTHORIZATION mode gets the caller's own PAT
    while self.ai stores nothing."""
    _patch_backend(monkeypatch, _FakeResponse(200, {"Content-Type": "text/event-stream"}, [b""]), captured)
    authenticated_user.post(
        "/mcp/echo",
        data=INIT_BODY,
        headers={
            "Accept": "application/json, text/event-stream",
            "X-Mcp-Authorization": "Bearer glpat-caller-own-token",
        },
    )
    fwd = captured["headers"]
    assert fwd["authorization"] == "Bearer glpat-caller-own-token"
    # ...and the header it arrived under is not also forwarded.
    assert "x-mcp-authorization" not in fwd


@pytest.mark.tier0
def test_backend_credential_scheme_is_verbatim(authenticated_user, captured, monkeypatch):
    """Basic works as readily as Bearer — mcp-mailbox wants HTTP Basic with an
    IPA user/password. The proxy does not parse or re-scheme the credential."""
    _patch_backend(monkeypatch, _FakeResponse(200, {"Content-Type": "text/event-stream"}, [b""]), captured)
    authenticated_user.post(
        "/mcp/echo",
        data=INIT_BODY,
        headers={"Accept": "application/json", "X-Mcp-Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert captured["headers"]["authorization"] == "Basic dXNlcjpwYXNz"


@pytest.mark.tier0
def test_no_backend_credential_means_no_authorization_at_all(authenticated_user, captured, monkeypatch):
    """Without X-Mcp-Authorization the backend gets no Authorization header —
    not the caller's, not a fallback, not an empty one. mcp-echo and
    playwright-mcp need none."""
    _patch_backend(monkeypatch, _FakeResponse(200, {"Content-Type": "text/event-stream"}, [b""]), captured)
    authenticated_user.post("/mcp/echo", data=INIT_BODY, headers={"Accept": "application/json"})
    assert "authorization" not in captured["headers"]


@pytest.mark.tier0
def test_unrecognised_and_identity_headers_are_dropped(authenticated_user, captured, monkeypatch):
    """Allowlisting means the identity headers of backends we have NOT wired
    cannot be spoofed through this proxy. mcp-taskspawn trusts X-Crew-Captain
    as a bare claim, protected only by a NetworkPolicy; if it is ever put behind
    this door, a caller must not be able to assert it. Same for a smuggled
    service-mesh ticket."""
    _patch_backend(monkeypatch, _FakeResponse(200, {"Content-Type": "text/event-stream"}, [b""]), captured)
    authenticated_user.post(
        "/mcp/echo",
        data=INIT_BODY,
        headers={
            "Accept": "application/json",
            "X-Crew-Captain": "numberone",
            "X-Crew-From": "krieger",
            "X-Selfai-Ticket": "a.forged.ticket",
            "X-Forwarded-For": "10.0.0.1",
        },
    )
    fwd = captured["headers"]
    for spoofed in ("x-crew-captain", "x-crew-from", "x-selfai-ticket", "x-forwarded-for"):
        assert spoofed not in fwd, f"{spoofed} reached the backend"


@pytest.mark.tier0
def test_mcp_transport_headers_still_pass(authenticated_user, captured, monkeypatch):
    """The allowlist must not have broken the transport: the session/protocol
    triple and User-Agent still reach the backend."""
    _patch_backend(monkeypatch, _FakeResponse(200, {"Content-Type": "text/event-stream"}, [b""]), captured)
    authenticated_user.post(
        "/mcp/echo",
        data=INIT_BODY,
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Mcp-Session-Id": "sess-abc",
            "Mcp-Protocol-Version": "2025-06-18",
            "Last-Event-ID": "42",
            "User-Agent": "probe/1.0",
        },
    )
    fwd = captured["headers"]
    assert fwd["mcp-session-id"] == "sess-abc"
    assert fwd["mcp-protocol-version"] == "2025-06-18"
    assert fwd["last-event-id"] == "42"
    assert fwd["content-type"] == "application/json"
    assert fwd["user-agent"] == "probe/1.0"


@pytest.mark.tier0
def test_get_and_delete_pass_through(authenticated_user, captured, monkeypatch):
    """GET (server→client notifications) and DELETE (session teardown) are
    forwarded with their method and Mcp-Session-Id intact."""
    _patch_backend(
        monkeypatch,
        _FakeResponse(200, {"Content-Type": "text/event-stream", "mcp-session-id": "sess-abc"}, [b""]),
        captured,
    )
    # DELETE: backend returns 204 bodyless; relay the status through.
    captured_delete = {}
    _patch_backend(
        monkeypatch,
        _FakeResponse(204, {}, []),
        captured_delete,
    )
    resp = authenticated_user.delete("/mcp/echo", headers={"Mcp-Session-Id": "sess-abc"})
    assert resp.status_code == 204
    assert captured_delete["method"] == "DELETE"
    assert captured_delete["headers"]["mcp-session-id"] == "sess-abc"

    # GET: long-lived notifications stream.
    captured_get = {}
    _patch_backend(
        monkeypatch,
        _FakeResponse(200, {"Content-Type": "text/event-stream", "mcp-session-id": "sess-abc"}, [b""]),
        captured_get,
    )
    resp = authenticated_user.get("/mcp/echo", headers={"Mcp-Session-Id": "sess-abc"})
    assert resp.status_code == 200
    assert captured_get["method"] == "GET"


@pytest.mark.tier0
def test_backend_unreachable_is_502(authenticated_user, captured, monkeypatch):
    """A backend connection failure surfaces as 502, not a 500 — the proxy
    names the failure rather than crashing."""

    async def failing_request(self, method, url, **kwargs):
        raise aiohttp.ClientError("backend down")

    monkeypatch.setattr(aiohttp.ClientSession, "request", failing_request)
    resp = authenticated_user.post("/mcp/echo", data=INIT_BODY)
    assert resp.status_code == 502


@pytest.mark.tier0
def test_admin_user_is_also_admitted(authenticated_admin, captured, monkeypatch):
    """Any verified user (user OR admin role) can use the front door — there is
    no extra scope gate on the proxy itself (per the captain: only authed users
    reach the MCPs)."""
    _patch_backend(
        monkeypatch,
        _FakeResponse(200, {"Content-Type": "text/event-stream", "mcp-session-id": "s"}, [b""]),
        captured,
    )
    resp = authenticated_admin.post(
        "/mcp/echo", data=INIT_BODY, headers={"Accept": "application/json, text/event-stream"}
    )
    assert resp.status_code == 200
