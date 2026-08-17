"""SpeakReleaseTransport response-mapping unit tests
(cavekit-vram-speak-consumer R3 / T-005) — a near-verbatim mirror of
``test_vram_llamolotl_transport.py`` re-pointed at ``SpeakReleaseTransport``.

Network-less and GPU-less: every case drives the concrete transport with a FAKE
httpx client (injected ``client_factory``) and a FAKE registry (for the pre-call
held), so nothing here touches a real socket or a live self.speak. Only the
adapter's HTTP-reply → :class:`ReleaseResponse` mapping is under test.

The one shape difference from the llamolotl copy: self.speak's control base is a
SINGLE STRING on ``config.TTS_CONTROL_BASE_URL`` (not a
``LLAMOLOTL_CONTROL_BASE_URLS`` list). The unconfigured cases therefore use
``TTS_CONTROL_BASE_URL=""`` and a missing/None value.

Also includes the T-003 dispatcher regression test: routing
``consumer_id="self.llamolotl"`` through the installed ``ConsumerAwareReleaseTransport``
must hit the LLAMOLOTL transport (llamolotl URL + audience ``self.llamolotl``),
and ``consumer_id="self.speak"`` must hit the SPEAK transport (speak URL +
audience ``self.speak``). A dispatcher that sent llamolotl to speak fails here.

Async cases run via ``asyncio.run(...)`` inside sync tests, matching the repo's
release-protocol tests (no pytest-asyncio marker; --strict-markers).
"""

import asyncio
from types import SimpleNamespace

import pytest

from selfai_ui.utils.vram_broker import ReleaseOutcome
from selfai_ui.utils.vram_speak import (
    RELEASE_PATH,
    RELEASE_SCOPE,
    ConsumerAwareReleaseTransport,
    SpeakReleaseTransport,
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


def _app_state(control_base):
    """self.speak's control base is the SINGLE STRING ``TTS_CONTROL_BASE_URL``
    (NOT a list). Pass ``""`` or ``None`` for the unconfigured guard cases."""
    return SimpleNamespace(
        config=SimpleNamespace(TTS_CONTROL_BASE_URL=control_base)
    )


def _make_transport(control_base, held, client, monkeypatch, minted=None):
    """Build a transport with fakes and stub out ticket minting so no shared
    secret is needed. ``minted`` (a list) captures the (audience, scope) calls."""
    if minted is not None:
        def fake_mint(audience, scope, *a, **k):
            minted.append((audience, scope))
            return "fake.ticket"

        monkeypatch.setattr(
            "selfai_ui.utils.vram_speak.mint_service_ticket", fake_mint
        )
    return SpeakReleaseTransport(
        app_state=_app_state(control_base),
        registry=FakeRegistry(held),
        client_factory=_factory_for(client),
    )


def _run(transport, consumer_id="self.speak", amount=4 * GiB, timeout=1.0):
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
        "http://self-speak:8880", {"self.speak": 10 * GiB}, client, monkeypatch, minted
    )

    resp = _run(transport)

    assert resp.outcome == ReleaseOutcome.CONFIRMED
    assert resp.new_held_bytes == 6 * GiB
    # POSTed to the right endpoint with the amount-based body.
    assert len(client.posted) == 1
    call = client.posted[0]
    assert call["url"] == f"http://self-speak:8880{RELEASE_PATH}"
    # Default (cooperative) release carries force=False explicitly.
    assert call["json"] == {
        "target_bytes": 4 * GiB,
        "timeout_seconds": 1.0,
        "force": False,
    }
    # Ticket minted for the speak audience with system:write.
    assert minted == [("self.speak", RELEASE_SCOPE)]


def test_partial_status_maps_to_confirmed_less_freed_than_asked(monkeypatch):
    # Asked for 4 GiB; consumer freed only 2 GiB (partial). Still confirmed.
    client = FakeClient(
        response=FakeResponse(200, {"status": "partial", "freed_bytes": 2 * GiB})
    )
    transport = _make_transport(
        "http://sp:8880", {"self.speak": 10 * GiB}, client, monkeypatch, []
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
        "http://sp:8880", {"self.speak": 10 * GiB}, client, monkeypatch, []
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
        "http://sp:8880", {}, client, monkeypatch, []  # empty registry
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
        "http://sp:8880", {"self.speak": 10 * GiB}, client, monkeypatch, []
    )

    resp = _run(transport)

    assert resp.outcome == ReleaseOutcome.TIMEOUT
    assert resp.new_held_bytes is None


def test_in_band_busy_status_maps_to_timeout(monkeypatch):
    # 200 OK but status=busy — still "not now", still TIMEOUT (not a denial).
    client = FakeClient(response=FakeResponse(200, {"status": "busy", "freed_bytes": 0}))
    transport = _make_transport(
        "http://sp:8880", {"self.speak": 10 * GiB}, client, monkeypatch, []
    )

    assert _run(transport).outcome == ReleaseOutcome.TIMEOUT


def test_non_2xx_maps_to_timeout(monkeypatch):
    client = FakeClient(response=FakeResponse(500, {"status": "released", "freed_bytes": 4 * GiB}))
    transport = _make_transport(
        "http://sp:8880", {"self.speak": 10 * GiB}, client, monkeypatch, []
    )

    assert _run(transport).outcome == ReleaseOutcome.TIMEOUT


def test_connection_error_maps_to_timeout(monkeypatch):
    client = FakeClient(raise_exc=OSError("connection refused"))
    transport = _make_transport(
        "http://sp:8880", {"self.speak": 10 * GiB}, client, monkeypatch, []
    )

    assert _run(transport).outcome == ReleaseOutcome.TIMEOUT


def test_undecodable_body_maps_to_timeout(monkeypatch):
    client = FakeClient(response=FakeResponse(200, raise_on_json=True))
    transport = _make_transport(
        "http://sp:8880", {"self.speak": 10 * GiB}, client, monkeypatch, []
    )

    assert _run(transport).outcome == ReleaseOutcome.TIMEOUT


def test_non_object_body_maps_to_timeout(monkeypatch):
    client = FakeClient(response=FakeResponse(200, ["not", "an", "object"]))
    transport = _make_transport(
        "http://sp:8880", {"self.speak": 10 * GiB}, client, monkeypatch, []
    )

    assert _run(transport).outcome == ReleaseOutcome.TIMEOUT


def test_confirmed_status_but_missing_freed_bytes_maps_to_timeout(monkeypatch):
    # status says released but no freed_bytes -> unrecognised, never guess.
    client = FakeClient(response=FakeResponse(200, {"status": "released"}))
    transport = _make_transport(
        "http://sp:8880", {"self.speak": 10 * GiB}, client, monkeypatch, []
    )

    assert _run(transport).outcome == ReleaseOutcome.TIMEOUT


def test_unrecognised_status_maps_to_timeout(monkeypatch):
    client = FakeClient(response=FakeResponse(200, {"status": "banana", "freed_bytes": 4 * GiB}))
    transport = _make_transport(
        "http://sp:8880", {"self.speak": 10 * GiB}, client, monkeypatch, []
    )

    assert _run(transport).outcome == ReleaseOutcome.TIMEOUT


def test_freed_bytes_bool_is_not_treated_as_int(monkeypatch):
    # bool is an int subclass; a stray `true` must NOT be read as freed=1.
    client = FakeClient(response=FakeResponse(200, {"status": "released", "freed_bytes": True}))
    transport = _make_transport(
        "http://sp:8880", {"self.speak": 10 * GiB}, client, monkeypatch, []
    )

    assert _run(transport).outcome == ReleaseOutcome.TIMEOUT


# ---------------------------------------------------------------------------
# Config guard + auth guard — clean TIMEOUT, no HTTP, never a silent success
# ---------------------------------------------------------------------------


def test_empty_control_base_maps_to_timeout_without_http(monkeypatch):
    client = FakeClient(response=FakeResponse(200, {"status": "released", "freed_bytes": 4 * GiB}))
    transport = _make_transport(
        "", {"self.speak": 10 * GiB}, client, monkeypatch, []  # empty control base
    )

    resp = _run(transport)

    assert resp.outcome == ReleaseOutcome.TIMEOUT
    # No HTTP attempted at all.
    assert client.posted == []


def test_whitespace_control_base_maps_to_timeout_without_http(monkeypatch):
    client = FakeClient(response=FakeResponse(200, {"status": "released", "freed_bytes": 4 * GiB}))
    transport = _make_transport(
        "   ", {"self.speak": 10 * GiB}, client, monkeypatch, []  # whitespace-only
    )

    assert _run(transport).outcome == ReleaseOutcome.TIMEOUT
    assert client.posted == []


def test_none_control_base_maps_to_timeout(monkeypatch):
    client = FakeClient(response=FakeResponse(200, {"status": "released", "freed_bytes": 4 * GiB}))
    transport = _make_transport(
        None, {"self.speak": 10 * GiB}, client, monkeypatch, []
    )

    assert _run(transport).outcome == ReleaseOutcome.TIMEOUT
    assert client.posted == []


def test_ticket_mint_failure_maps_to_timeout_without_http(monkeypatch):
    client = FakeClient(response=FakeResponse(200, {"status": "released", "freed_bytes": 4 * GiB}))
    # Do NOT stub mint here; instead make it raise (secret unconfigured).
    def boom(audience, scope, *a, **k):
        raise RuntimeError("SERVICE_AUTH_SECRET not configured")

    monkeypatch.setattr("selfai_ui.utils.vram_speak.mint_service_ticket", boom)
    transport = SpeakReleaseTransport(
        app_state=_app_state("http://sp:8880"),
        registry=FakeRegistry({"self.speak": 10 * GiB}),
        client_factory=_factory_for(client),
    )

    resp = _run(transport)

    assert resp.outcome == ReleaseOutcome.TIMEOUT
    # Never called the endpoint unauthenticated.
    assert client.posted == []


# ---------------------------------------------------------------------------
# T-003 — the production-critical dispatcher regression test.
#
# The broker holds ONE global transport and calls request_release(consumer_id)
# without routing by consumer_id. The installed ConsumerAwareReleaseTransport
# must route self.speak -> speak transport and everything else -> llamolotl
# transport. A dispatcher that sent llamolotl's release to the speak endpoint
# would break live VRAM arbitration in production — this test fails it.
# ---------------------------------------------------------------------------


def _make_dispatcher(monkeypatch, speak_client, llamolotl_client, speak_minted, ll_minted):
    """Build the real ConsumerAwareReleaseTransport wrapping a real
    SpeakReleaseTransport (speak app_state) and a real LlamolotlReleaseTransport
    (llamolotl app_state), each with its own fake client and mint capture."""
    from selfai_ui.utils.vram_llamolotl import LlamolotlReleaseTransport

    def speak_mint(audience, scope, *a, **k):
        speak_minted.append((audience, scope))
        return "speak.ticket"

    def ll_mint(audience, scope, *a, **k):
        ll_minted.append((audience, scope))
        return "ll.ticket"

    monkeypatch.setattr("selfai_ui.utils.vram_speak.mint_service_ticket", speak_mint)
    monkeypatch.setattr("selfai_ui.utils.vram_llamolotl.mint_service_ticket", ll_mint)

    speak_transport = SpeakReleaseTransport(
        app_state=_app_state("http://self-speak:8880"),
        registry=FakeRegistry({"self.speak": 1 * GiB}),
        client_factory=_factory_for(speak_client),
    )
    llamolotl_transport = LlamolotlReleaseTransport(
        app_state=SimpleNamespace(
            config=SimpleNamespace(LLAMOLOTL_CONTROL_BASE_URLS=["http://self-llamolotl:8093"])
        ),
        registry=FakeRegistry({"self.llamolotl": 10 * GiB}),
        client_factory=_factory_for(llamolotl_client),
    )
    return ConsumerAwareReleaseTransport(
        speak_transport=speak_transport,
        llamolotl_transport=llamolotl_transport,
    )


def test_dispatcher_routes_llamolotl_to_llamolotl_transport(monkeypatch):
    # THE regression: a self.llamolotl release must reach the LLAMOLOTL endpoint
    # with the self.llamolotl audience — never self.speak.
    speak_client = FakeClient(
        response=FakeResponse(200, {"status": "released", "freed_bytes": 1 * GiB})
    )
    llamolotl_client = FakeClient(
        response=FakeResponse(200, {"status": "released", "freed_bytes": 4 * GiB})
    )
    speak_minted, ll_minted = [], []
    dispatcher = _make_dispatcher(
        monkeypatch, speak_client, llamolotl_client, speak_minted, ll_minted
    )

    resp = asyncio.run(
        dispatcher.request_release("self.llamolotl", 4 * GiB, 1.0)
    )

    assert resp.outcome == ReleaseOutcome.CONFIRMED
    assert resp.new_held_bytes == 6 * GiB  # 10 - 4, from the llamolotl registry
    # Hit the llamolotl endpoint + audience, NOT self.speak.
    assert len(llamolotl_client.posted) == 1
    assert llamolotl_client.posted[0]["url"] == f"http://self-llamolotl:8093{RELEASE_PATH}"
    assert ll_minted == [("self.llamolotl", "system:write")]
    # The speak transport must be completely untouched.
    assert speak_client.posted == []
    assert speak_minted == []


def test_dispatcher_routes_speak_to_speak_transport(monkeypatch):
    # A self.speak release must reach the SPEAK endpoint with the self.speak
    # audience — never llamolotl.
    speak_client = FakeClient(
        response=FakeResponse(200, {"status": "released", "freed_bytes": 1 * GiB})
    )
    llamolotl_client = FakeClient(
        response=FakeResponse(200, {"status": "released", "freed_bytes": 4 * GiB})
    )
    speak_minted, ll_minted = [], []
    dispatcher = _make_dispatcher(
        monkeypatch, speak_client, llamolotl_client, speak_minted, ll_minted
    )

    resp = asyncio.run(
        dispatcher.request_release("self.speak", 512 * 1024 * 1024, 2.0)
    )

    assert resp.outcome == ReleaseOutcome.CONFIRMED
    # Hit the speak endpoint + audience + scope, NOT llamolotl.
    assert len(speak_client.posted) == 1
    assert speak_client.posted[0]["url"] == f"http://self-speak:8880{RELEASE_PATH}"
    assert speak_client.posted[0]["json"] == {
        "target_bytes": 512 * 1024 * 1024,
        "timeout_seconds": 2.0,
        "force": False,
    }
    assert speak_minted == [("self.speak", RELEASE_SCOPE)]
    # The llamolotl transport must be completely untouched.
    assert llamolotl_client.posted == []
    assert ll_minted == []


def test_force_true_is_forwarded_in_release_body(monkeypatch):
    # Admin e-stop: force=True must appear in the POST body so self.speak skips
    # its drain-wait and unloads both engines immediately.
    client = FakeClient(
        response=FakeResponse(200, {"status": "released", "freed_bytes": 4 * GiB})
    )
    minted = []
    transport = _make_transport(
        "http://self-speak:8880", {"self.speak": 10 * GiB}, client, monkeypatch, minted
    )

    resp = asyncio.run(
        transport.request_release("self.speak", 4 * GiB, 1.0, force=True)
    )

    assert resp.outcome == ReleaseOutcome.CONFIRMED
    assert client.posted[0]["json"] == {
        "target_bytes": 4 * GiB,
        "timeout_seconds": 1.0,
        "force": True,
    }


def test_dispatcher_forwards_force_to_speak_transport(monkeypatch):
    # The consumer-aware dispatcher must forward force through to the per-consumer
    # transport (the e-stop calls the dispatcher, not each transport directly).
    speak_client = FakeClient(
        response=FakeResponse(200, {"status": "released", "freed_bytes": 1 * GiB})
    )
    llamolotl_client = FakeClient(
        response=FakeResponse(200, {"status": "released", "freed_bytes": 4 * GiB})
    )
    speak_minted, ll_minted = [], []
    dispatcher = _make_dispatcher(
        monkeypatch, speak_client, llamolotl_client, speak_minted, ll_minted
    )

    asyncio.run(
        dispatcher.request_release("self.speak", 1 * GiB, 2.0, force=True)
    )

    assert speak_client.posted[0]["json"]["force"] is True


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
