"""T-008 — LlamolotlVramStateSource.read_held_bytes parse-matrix unit tests.

Network-less: every case drives the concrete state source with a FAKE httpx
client (injected ``client_factory``), so nothing here touches a real socket or
a live llamolotl. Only the adapter's HTTP-reply → held-bytes parse is under
test (the poller-side heartbeat relaying is covered by test_vram_poller.py).

This suite exists specifically because T-011 exercises only a *fake* source, so
the real adapter's defensive parse — and above all the exact reply field name
``held_vram_bytes`` — had no repeatable regression test. A field-name drift
(the adapter once read ``held_bytes``, which the endpoint never returns, making
the whole poller a silent no-op) would slip past py_compile + a fake-source
poller test; the ``held_vram_bytes`` case below is the guard against that.

Async cases run via ``asyncio.run(...)`` inside sync tests, matching
``test_vram_llamolotl_transport.py`` (the repo registers no pytest-asyncio
marker and runs under --strict-markers).
"""

import asyncio
from types import SimpleNamespace

from selfai_ui.utils.vram_llamolotl import (
    STATE_PATH,
    STATE_SCOPE,
    LlamolotlVramStateSource,
)

GiB = 1024**3


# ---------------------------------------------------------------------------
# Fakes: httpx response, httpx client (GET), app_state
# ---------------------------------------------------------------------------


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
    GET, returns a preprogrammed response, or raises a preprogrammed error."""

    def __init__(self, response=None, raise_exc=None):
        self._response = response
        self._raise = raise_exc
        self.gets = []
        self.timeout = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        self.gets.append({"url": url, "headers": headers})
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


def _make_source(control_urls, client, monkeypatch, minted=None):
    """Build a state source with fakes and stub ticket minting so no shared
    secret is needed. ``minted`` (a list) captures the (audience, scope) calls."""
    if minted is not None:
        def fake_mint(audience, scope, *a, **k):
            minted.append((audience, scope))
            return "fake.ticket"

        monkeypatch.setattr(
            "selfai_ui.utils.vram_llamolotl.mint_service_ticket", fake_mint
        )
    return LlamolotlVramStateSource(
        app_state=_app_state(control_urls),
        client_factory=_factory_for(client),
    )


def _read(source, consumer_id="self.llamolotl"):
    return asyncio.run(source.read_held_bytes(consumer_id))


# ---------------------------------------------------------------------------
# The regression guard: the endpoint's real field name is held_vram_bytes
# ---------------------------------------------------------------------------


def test_held_vram_bytes_is_read__the_endpoints_real_field(monkeypatch):
    """The live self.llamolotl VramStateResponse returns ``held_vram_bytes``.
    This is THE field the adapter must read; if it silently stops matching it,
    the poller no-ops with no other failing test to catch it."""
    client = FakeClient(FakeResponse(200, {
        "held_vram_bytes": 15 * GiB,
        "total_capacity_bytes": 24 * GiB,
        "gpu_reachable": True,
        "router_reachable": True,
        "status": "ok",
    }))
    source = _make_source(["http://llamolotl:8093"], client, monkeypatch)
    assert _read(source) == 15 * GiB
    # It really made the GET to the state path with the ticket header.
    assert client.gets[0]["url"] == "http://llamolotl:8093" + STATE_PATH
    assert "X-Selfai-Ticket" in client.gets[0]["headers"]


def test_state_read_mints_system_read_ticket(monkeypatch):
    minted = []
    client = FakeClient(FakeResponse(200, {"held_vram_bytes": 1 * GiB}))
    source = _make_source(["http://llamolotl:8093"], client, monkeypatch, minted=minted)
    assert _read(source) == 1 * GiB
    assert minted == [("self.llamolotl", STATE_SCOPE)]


# ---------------------------------------------------------------------------
# Defensive fallbacks (held_bytes / held) and precedence
# ---------------------------------------------------------------------------


def test_held_bytes_fallback_is_read(monkeypatch):
    client = FakeClient(FakeResponse(200, {"held_bytes": 8 * GiB}))
    source = _make_source(["http://llamolotl:8093"], client, monkeypatch)
    assert _read(source) == 8 * GiB


def test_held_fallback_is_read(monkeypatch):
    client = FakeClient(FakeResponse(200, {"held": 3 * GiB}))
    source = _make_source(["http://llamolotl:8093"], client, monkeypatch)
    assert _read(source) == 3 * GiB


def test_held_vram_bytes_wins_over_fallbacks(monkeypatch):
    """Precedence matters: the authoritative field must win if both are present
    (a mixed reply must not resolve to a stale fallback)."""
    client = FakeClient(FakeResponse(200, {
        "held_vram_bytes": 15 * GiB,
        "held_bytes": 1 * GiB,  # a decoy that must NOT win
    }))
    source = _make_source(["http://llamolotl:8093"], client, monkeypatch)
    assert _read(source) == 15 * GiB


def test_zero_held_is_a_real_reading_not_none(monkeypatch):
    """A genuine near-zero (no models resident) is a real 0, distinct from the
    None the adapter returns when it cannot read at all."""
    client = FakeClient(FakeResponse(200, {"held_vram_bytes": 0}))
    source = _make_source(["http://llamolotl:8093"], client, monkeypatch)
    assert _read(source) == 0


# ---------------------------------------------------------------------------
# Every failure shape → None (never a raise, never a fabricated value)
# ---------------------------------------------------------------------------


def test_bool_held_is_not_treated_as_int(monkeypatch):
    """bool is an int subclass; a stray ``true`` must not be read as 1."""
    client = FakeClient(FakeResponse(200, {"held_vram_bytes": True}))
    source = _make_source(["http://llamolotl:8093"], client, monkeypatch)
    assert _read(source) is None


def test_no_integer_held_field_maps_to_none(monkeypatch):
    client = FakeClient(FakeResponse(200, {"total_capacity_bytes": 24 * GiB, "status": "ok"}))
    source = _make_source(["http://llamolotl:8093"], client, monkeypatch)
    assert _read(source) is None


def test_non_2xx_maps_to_none(monkeypatch):
    client = FakeClient(FakeResponse(503, {"held_vram_bytes": 15 * GiB}))
    source = _make_source(["http://llamolotl:8093"], client, monkeypatch)
    assert _read(source) is None


def test_connection_error_maps_to_none(monkeypatch):
    client = FakeClient(raise_exc=OSError("connection refused"))
    source = _make_source(["http://llamolotl:8093"], client, monkeypatch)
    assert _read(source) is None


def test_undecodable_body_maps_to_none(monkeypatch):
    client = FakeClient(FakeResponse(200, raise_on_json=True))
    source = _make_source(["http://llamolotl:8093"], client, monkeypatch)
    assert _read(source) is None


def test_non_object_body_maps_to_none(monkeypatch):
    client = FakeClient(FakeResponse(200, [15 * GiB]))  # a list, not an object
    source = _make_source(["http://llamolotl:8093"], client, monkeypatch)
    assert _read(source) is None


# ---------------------------------------------------------------------------
# Config / ticket guards → None WITHOUT any HTTP attempt
# ---------------------------------------------------------------------------


def test_unconfigured_control_url_maps_to_none_without_http(monkeypatch):
    """AC3: no configured control base = "no source" — the poller skips. No
    GET is attempted."""
    client = FakeClient(FakeResponse(200, {"held_vram_bytes": 15 * GiB}))
    source = _make_source([], client, monkeypatch)
    assert _read(source) is None
    assert client.gets == []  # never hit the network


def test_none_control_url_maps_to_none_without_http(monkeypatch):
    client = FakeClient(FakeResponse(200, {"held_vram_bytes": 15 * GiB}))
    source = _make_source(None, client, monkeypatch)
    assert _read(source) is None
    assert client.gets == []


def test_ticket_mint_failure_maps_to_none_without_http(monkeypatch):
    client = FakeClient(FakeResponse(200, {"held_vram_bytes": 15 * GiB}))

    def boom(audience, scope, *a, **k):
        raise RuntimeError("SERVICE_AUTH_SECRET unset")

    monkeypatch.setattr("selfai_ui.utils.vram_llamolotl.mint_service_ticket", boom)
    source = LlamolotlVramStateSource(
        app_state=_app_state(["http://llamolotl:8093"]),
        client_factory=_factory_for(client),
    )
    assert _read(source) is None
    assert client.gets == []  # ticket failed before any GET
