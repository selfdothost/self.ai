"""T-016 — LlamolotlReleaseTransport response-mapping unit tests.

Network-less and GPU-less: every case drives the concrete transport with a
FAKE httpx client (injected ``client_factory``) and a FAKE registry (for the
pre-call held), so nothing here touches a real socket or a live llamolotl. Only
the adapter's HTTP-reply → :class:`ReleaseResponse` mapping is under test (the
broker-side registry writes are covered by T-011).

Async cases run via ``asyncio.run(...)`` inside sync tests, matching
``test_vram_release_protocol.py`` (the repo registers no pytest-asyncio marker
and runs under --strict-markers).
"""

import asyncio
from types import SimpleNamespace

import pytest

from selfai_ui.utils.vram_broker import ReleaseOutcome
from selfai_ui.utils.vram_llamolotl import (
    RELEASE_PATH,
    RELEASE_SCOPE,
    LlamolotlReleaseTransport,
)

GiB = 1024**3


# ---------------------------------------------------------------------------
# Fakes: registry (pre-call held), httpx response, httpx client, app_state
# ---------------------------------------------------------------------------


class FakeRegistry:
    """Minimal registry stub: only ``get`` is used by the transport."""

    def __init__(self, held_by_id):
        self._held = held_by_id

    def get(self, consumer_id):
        held = self._held.get(consumer_id)
        if held is None:
            return None
        return SimpleNamespace(consumer_id=consumer_id, held_bytes=held)


_UNSET = object()


class FakeResponse:
    def __init__(self, status_code, json_data=_UNSET, raise_on_json=False):
        self.status_code = status_code
        self._json = json_data
        self._raise_on_json = raise_on_json

    def json(self):
        if self._raise_on_json:
            raise ValueError("body is not valid JSON")
        if self._json is _UNSET:
            raise ValueError("no body")
        return self._json


class FakeClient:
    """Async-context-manager stand-in for ``httpx.AsyncClient``. Records the
    POST, returns a preprogrammed response, or raises a preprogrammed error."""

    def __init__(self, response=None, raise_exc=None):
        self._response = response
        self._raise = raise_exc
        self.posted = []
        self.timeout = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        self.posted.append({"url": url, "json": json, "headers": headers})
        if self._raise is not None:
            raise self._raise
        return self._response


def _factory_for(client):
    def factory(timeout):
        client.timeout = timeout
        return client

    return factory


def _app_state(control_urls):
    return SimpleNamespace(
        config=SimpleNamespace(LLAMOLOTL_CONTROL_BASE_URLS=control_urls)
    )


def _make_transport(control_urls, held, client, monkeypatch, minted=None):
    """Build a transport with fakes and stub out ticket minting so no shared
    secret is needed. ``minted`` (a list) captures the (audience, scope) calls."""
    if minted is not None:
        def fake_mint(audience, scope, *a, **k):
            minted.append((audience, scope))
            return "fake.ticket"

        monkeypatch.setattr(
            "selfai_ui.utils.vram_llamolotl.mint_service_ticket", fake_mint
        )
    return LlamolotlReleaseTransport(
        app_state=_app_state(control_urls),
        registry=FakeRegistry(held),
        client_factory=_factory_for(client),
    )


def _run(transport, consumer_id="self.llamolotl", amount=4 * GiB, timeout=1.0):
    return asyncio.run(transport.request_release(consumer_id, amount, timeout))


# ---------------------------------------------------------------------------
# CONFIRMED — a real release
# ---------------------------------------------------------------------------


def test_released_status_maps_to_confirmed_with_new_held(monkeypatch):
    # held 10 GiB, freed 4 GiB -> new held 6 GiB.
    client = FakeClient(
        response=FakeResponse(200, {"status": "released", "freed_bytes": 4 * GiB})
    )
    minted = []
    transport = _make_transport(
        ["http://self-llamolotl:8093"], {"self.llamolotl": 10 * GiB}, client, monkeypatch, minted
    )

    resp = _run(transport)

    assert resp.outcome == ReleaseOutcome.CONFIRMED
    assert resp.new_held_bytes == 6 * GiB
    # POSTed to the right endpoint with the amount-based body.
    assert len(client.posted) == 1
    call = client.posted[0]
    assert call["url"] == f"http://self-llamolotl:8093{RELEASE_PATH}"
    # Default (cooperative) release carries force=False explicitly.
    assert call["json"] == {
        "target_bytes": 4 * GiB,
        "timeout_seconds": 1.0,
        "force": False,
    }
    # Ticket minted for the llamolotl audience with system:write.
    assert minted == [("self.llamolotl", RELEASE_SCOPE)]


def test_partial_status_maps_to_confirmed_less_freed_than_asked(monkeypatch):
    # Asked for 4 GiB; consumer freed only 2 GiB (partial). Still confirmed.
    client = FakeClient(
        response=FakeResponse(200, {"status": "partial", "freed_bytes": 2 * GiB})
    )
    transport = _make_transport(
        ["http://ll:8093"], {"self.llamolotl": 10 * GiB}, client, monkeypatch, []
    )

    resp = _run(transport)

    assert resp.outcome == ReleaseOutcome.CONFIRMED
    assert resp.new_held_bytes == 8 * GiB  # 10 - 2


def test_freed_more_than_held_clamps_new_held_to_zero(monkeypatch):
    # Consumer evicted a whole model, freed more than the registry knew it held.
    client = FakeClient(
        response=FakeResponse(200, {"status": "released", "freed_bytes": 12 * GiB})
    )
    transport = _make_transport(
        ["http://ll:8093"], {"self.llamolotl": 10 * GiB}, client, monkeypatch, []
    )

    resp = _run(transport)

    assert resp.outcome == ReleaseOutcome.CONFIRMED
    assert resp.new_held_bytes == 0  # max(0, 10 - 12)


def test_confirmed_with_unknown_pre_held_leaves_new_held_none(monkeypatch):
    # Registry has no row for the consumer -> cannot derive absolute new held.
    # Broker will treat a confirmed-without-held as unresolvable/stale.
    client = FakeClient(
        response=FakeResponse(200, {"status": "released", "freed_bytes": 4 * GiB})
    )
    transport = _make_transport(
        ["http://ll:8093"], {}, client, monkeypatch, []  # empty registry
    )

    resp = _run(transport)

    assert resp.outcome == ReleaseOutcome.CONFIRMED
    assert resp.new_held_bytes is None


# ---------------------------------------------------------------------------
# TIMEOUT — busy / non-2xx / errors / malformed (never a silent success)
# ---------------------------------------------------------------------------


def test_http_409_busy_maps_to_timeout(monkeypatch):
    client = FakeClient(response=FakeResponse(409, {"status": "busy"}))
    transport = _make_transport(
        ["http://ll:8093"], {"self.llamolotl": 10 * GiB}, client, monkeypatch, []
    )

    resp = _run(transport)

    assert resp.outcome == ReleaseOutcome.TIMEOUT
    assert resp.new_held_bytes is None


def test_in_band_busy_status_maps_to_timeout(monkeypatch):
    # 200 OK but status=busy — still "not now", still TIMEOUT (not a denial).
    client = FakeClient(response=FakeResponse(200, {"status": "busy", "freed_bytes": 0}))
    transport = _make_transport(
        ["http://ll:8093"], {"self.llamolotl": 10 * GiB}, client, monkeypatch, []
    )

    assert _run(transport).outcome == ReleaseOutcome.TIMEOUT


def test_non_2xx_maps_to_timeout(monkeypatch):
    client = FakeClient(response=FakeResponse(500, {"status": "released", "freed_bytes": 4 * GiB}))
    transport = _make_transport(
        ["http://ll:8093"], {"self.llamolotl": 10 * GiB}, client, monkeypatch, []
    )

    assert _run(transport).outcome == ReleaseOutcome.TIMEOUT


def test_connection_error_maps_to_timeout(monkeypatch):
    client = FakeClient(raise_exc=OSError("connection refused"))
    transport = _make_transport(
        ["http://ll:8093"], {"self.llamolotl": 10 * GiB}, client, monkeypatch, []
    )

    assert _run(transport).outcome == ReleaseOutcome.TIMEOUT


def test_undecodable_body_maps_to_timeout(monkeypatch):
    client = FakeClient(response=FakeResponse(200, raise_on_json=True))
    transport = _make_transport(
        ["http://ll:8093"], {"self.llamolotl": 10 * GiB}, client, monkeypatch, []
    )

    assert _run(transport).outcome == ReleaseOutcome.TIMEOUT


def test_non_object_body_maps_to_timeout(monkeypatch):
    client = FakeClient(response=FakeResponse(200, ["not", "an", "object"]))
    transport = _make_transport(
        ["http://ll:8093"], {"self.llamolotl": 10 * GiB}, client, monkeypatch, []
    )

    assert _run(transport).outcome == ReleaseOutcome.TIMEOUT


def test_confirmed_status_but_missing_freed_bytes_maps_to_timeout(monkeypatch):
    # status says released but no freed_bytes -> unrecognised, never guess.
    client = FakeClient(response=FakeResponse(200, {"status": "released"}))
    transport = _make_transport(
        ["http://ll:8093"], {"self.llamolotl": 10 * GiB}, client, monkeypatch, []
    )

    assert _run(transport).outcome == ReleaseOutcome.TIMEOUT


def test_unrecognised_status_maps_to_timeout(monkeypatch):
    client = FakeClient(response=FakeResponse(200, {"status": "banana", "freed_bytes": 4 * GiB}))
    transport = _make_transport(
        ["http://ll:8093"], {"self.llamolotl": 10 * GiB}, client, monkeypatch, []
    )

    assert _run(transport).outcome == ReleaseOutcome.TIMEOUT


def test_freed_bytes_bool_is_not_treated_as_int(monkeypatch):
    # bool is an int subclass; a stray `true` must NOT be read as freed=1.
    client = FakeClient(response=FakeResponse(200, {"status": "released", "freed_bytes": True}))
    transport = _make_transport(
        ["http://ll:8093"], {"self.llamolotl": 10 * GiB}, client, monkeypatch, []
    )

    assert _run(transport).outcome == ReleaseOutcome.TIMEOUT


# ---------------------------------------------------------------------------
# Config guard + auth guard — clean TIMEOUT, no HTTP, never a silent success
# ---------------------------------------------------------------------------


def test_unconfigured_control_url_maps_to_timeout_without_http(monkeypatch):
    client = FakeClient(response=FakeResponse(200, {"status": "released", "freed_bytes": 4 * GiB}))
    transport = _make_transport(
        [], {"self.llamolotl": 10 * GiB}, client, monkeypatch, []  # no control URL
    )

    resp = _run(transport)

    assert resp.outcome == ReleaseOutcome.TIMEOUT
    # No HTTP attempted at all.
    assert client.posted == []


def test_none_control_url_maps_to_timeout(monkeypatch):
    client = FakeClient(response=FakeResponse(200, {"status": "released", "freed_bytes": 4 * GiB}))
    transport = _make_transport(
        None, {"self.llamolotl": 10 * GiB}, client, monkeypatch, []
    )

    assert _run(transport).outcome == ReleaseOutcome.TIMEOUT
    assert client.posted == []


def test_ticket_mint_failure_maps_to_timeout_without_http(monkeypatch):
    client = FakeClient(response=FakeResponse(200, {"status": "released", "freed_bytes": 4 * GiB}))
    # Do NOT stub mint here; instead make it raise (secret unconfigured).
    def boom(audience, scope, *a, **k):
        raise RuntimeError("SERVICE_AUTH_SECRET not configured")

    monkeypatch.setattr("selfai_ui.utils.vram_llamolotl.mint_service_ticket", boom)
    transport = LlamolotlReleaseTransport(
        app_state=_app_state(["http://ll:8093"]),
        registry=FakeRegistry({"self.llamolotl": 10 * GiB}),
        client_factory=_factory_for(client),
    )

    resp = _run(transport)

    assert resp.outcome == ReleaseOutcome.TIMEOUT
    # Never called the endpoint unauthenticated.
    assert client.posted == []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
