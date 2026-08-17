"""CuratorReleaseTransport response mapping + dispatcher routing (self.ai#88).

Network-less and GPU-less, in the shape of ``test_vram_speak_transport.py``:
every case drives the concrete transport with a FAKE httpx client and a FAKE
registry, so nothing touches a socket or a live self.curator.

The behaviour that makes this transport different from its three siblings is
under test here:

  * an explained refusal maps to :data:`ReleaseOutcome.DENIED`, **not** the
    coerced TIMEOUT speak/sketch produce for a busy consumer, and the reason
    survives onto the ``ReleaseResponse``. The difference is load-bearing:
    DENIED leaves the consumer ``steady`` with its held known, TIMEOUT marks it
    ``stale`` — and a stale holder is force-reap eligible, i.e. a merely-busy
    curation pod would eventually have its pod deleted mid-pipeline;
  * an e-stop resolves ``new_held_bytes`` to 0 rather than ``pre - freed``,
    because a confirmed e-stop asserts nothing is left running;
  * ``force=True`` and ``force=False`` hit the same endpoint but must set the
    flag differently — a release that silently forced would kill running jobs.
"""

import asyncio
from types import SimpleNamespace

import pytest

from selfai_ui.utils.vram_broker import ReleaseOutcome
from selfai_ui.utils.vram_curator import (
    CURATOR_AUDIENCE,
    RELEASE_PATH,
    RELEASE_SCOPE,
    CuratorReleaseTransport,
)
from selfai_ui.utils.vram_speak import ConsumerAwareReleaseTransport

GiB = 1024**3

_UNSET = object()


class FakeRegistry:
    def __init__(self, held_by_id):
        self._held = held_by_id

    def get(self, consumer_id):
        held = self._held.get(consumer_id)
        if held is None:
            return None
        return SimpleNamespace(consumer_id=consumer_id, held_bytes=held)


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
    return SimpleNamespace(config=SimpleNamespace(CURATOR_CONTROL_BASE_URL=control_base))


def _make_transport(control_base, held, client, monkeypatch, minted=None):
    if minted is not None:

        def fake_mint(audience, scope, *a, **k):
            minted.append((audience, scope))
            return "fake.ticket"

        monkeypatch.setattr("selfai_ui.utils.vram_curator.mint_service_ticket", fake_mint)
    else:
        monkeypatch.setattr(
            "selfai_ui.utils.vram_curator.mint_service_ticket",
            lambda *a, **k: "fake.ticket",
        )
    return CuratorReleaseTransport(
        app_state=_app_state(control_base),
        registry=FakeRegistry(held),
        client_factory=_factory_for(client),
    )


# ── the polite ask ──────────────────────────────────────────────


def test_explained_refusal_is_denied_not_a_timeout(monkeypatch):
    """The headline difference from the sibling transports. A curation run that
    says "no, I'm mid-pipeline" is REFUSING, not silent — coercing that to a
    timeout would mark a healthy pod stale and make it force-reap eligible."""
    client = FakeClient(
        FakeResponse(
            200,
            {
                "status": "denied",
                "freed_bytes": 0,
                "reason": "curation pipeline in progress: job-a (running 400s)",
            },
        )
    )
    transport = _make_transport("http://curator:8094", {CURATOR_AUDIENCE: 8 * GiB}, client, monkeypatch)

    resp = asyncio.run(transport.request_release(CURATOR_AUDIENCE, 4 * GiB, 5.0))

    assert resp.outcome == ReleaseOutcome.DENIED
    assert "job-a" in resp.reason
    # A denial must never write a held — the broker leaves the old one standing.
    assert resp.new_held_bytes is None


def test_refusal_without_a_stated_reason_still_denies_and_says_so(monkeypatch):
    client = FakeClient(FakeResponse(200, {"status": "denied", "freed_bytes": 0}))
    transport = _make_transport("http://curator:8094", {CURATOR_AUDIENCE: 8 * GiB}, client, monkeypatch)

    resp = asyncio.run(transport.request_release(CURATOR_AUDIENCE, 4 * GiB, 5.0))

    assert resp.outcome == ReleaseOutcome.DENIED
    assert resp.reason  # never None — the breakdown should not show a blank


def test_bare_busy_is_still_a_timeout(monkeypatch):
    """`busy` with nothing to say is closer to an unknown than to a refusal, so
    it keeps the siblings' mapping. self.curator does not emit it; an older
    build might."""
    client = FakeClient(FakeResponse(200, {"status": "busy"}))
    transport = _make_transport("http://curator:8094", {CURATOR_AUDIENCE: 8 * GiB}, client, monkeypatch)

    resp = asyncio.run(transport.request_release(CURATOR_AUDIENCE, 4 * GiB, 5.0))

    assert resp.outcome == ReleaseOutcome.TIMEOUT


def test_confirmed_release_subtracts_freed_from_the_pre_call_held(monkeypatch):
    client = FakeClient(FakeResponse(200, {"status": "released", "freed_bytes": 3 * GiB}))
    transport = _make_transport("http://curator:8094", {CURATOR_AUDIENCE: 8 * GiB}, client, monkeypatch)

    resp = asyncio.run(transport.request_release(CURATOR_AUDIENCE, 3 * GiB, 5.0))

    assert resp.outcome == ReleaseOutcome.CONFIRMED
    assert resp.new_held_bytes == 5 * GiB


def test_confirmed_release_with_unknown_pre_held_is_unresolvable(monkeypatch):
    """No pre-call held means we cannot compute an absolute new held. Returning
    None makes the broker treat it as unknown/stale — never a silent success."""
    client = FakeClient(FakeResponse(200, {"status": "released", "freed_bytes": 3 * GiB}))
    transport = _make_transport("http://curator:8094", {}, client, monkeypatch)

    resp = asyncio.run(transport.request_release(CURATOR_AUDIENCE, 3 * GiB, 5.0))

    assert resp.outcome == ReleaseOutcome.CONFIRMED
    assert resp.new_held_bytes is None


def test_polite_ask_does_not_set_force(monkeypatch):
    """A release that quietly forced would terminate a running pipeline behind
    the operator's back."""
    client = FakeClient(FakeResponse(200, {"status": "denied", "reason": "busy"}))
    transport = _make_transport("http://curator:8094", {CURATOR_AUDIENCE: GiB}, client, monkeypatch)

    asyncio.run(transport.request_release(CURATOR_AUDIENCE, GiB, 5.0))

    assert client.posted[0]["json"]["force"] is False
    assert client.posted[0]["url"] == f"http://curator:8094{RELEASE_PATH}"


@pytest.mark.parametrize(
    "response,exc",
    [
        (FakeResponse(500, {"status": "released", "freed_bytes": 1}), None),
        (FakeResponse(200, raise_on_json=True), None),
        (FakeResponse(200, ["not", "an", "object"]), None),
        (FakeResponse(200, {"status": "released"}), None),  # no freed_bytes
        (FakeResponse(200, {"status": "released", "freed_bytes": True}), None),  # bool
        (FakeResponse(200, {"status": "who-knows", "freed_bytes": 1}), None),
        (None, RuntimeError("connection refused")),
    ],
)
def test_every_unrecognisable_shape_is_a_timeout_never_a_success(
    response, exc, monkeypatch
):
    client = FakeClient(response, raise_exc=exc)
    transport = _make_transport("http://curator:8094", {CURATOR_AUDIENCE: 8 * GiB}, client, monkeypatch)

    resp = asyncio.run(transport.request_release(CURATOR_AUDIENCE, GiB, 5.0))

    assert resp.outcome == ReleaseOutcome.TIMEOUT
    assert resp.new_held_bytes is None


@pytest.mark.parametrize("control_base", ["", "   ", None])
def test_unconfigured_control_base_makes_no_http_call(control_base, monkeypatch):
    client = FakeClient(FakeResponse(200, {"status": "released", "freed_bytes": 1}))
    transport = _make_transport(control_base, {CURATOR_AUDIENCE: GiB}, client, monkeypatch)

    resp = asyncio.run(transport.request_release(CURATOR_AUDIENCE, GiB, 5.0))

    assert resp.outcome == ReleaseOutcome.TIMEOUT
    assert client.posted == []


def test_unmintable_ticket_never_calls_the_endpoint_unauthenticated(monkeypatch):
    """Critical on the force path: that endpoint kills a running pipeline."""
    client = FakeClient(FakeResponse(200, {"status": "released", "freed_bytes": 1}))

    def _boom(*a, **k):
        raise RuntimeError("SERVICE_AUTH_SECRET unset")

    monkeypatch.setattr("selfai_ui.utils.vram_curator.mint_service_ticket", _boom)
    transport = CuratorReleaseTransport(
        app_state=_app_state("http://curator:8094"),
        registry=FakeRegistry({CURATOR_AUDIENCE: GiB}),
        client_factory=_factory_for(client),
    )

    resp = asyncio.run(transport.request_release(CURATOR_AUDIENCE, 0, 5.0, force=True))

    assert resp.outcome == ReleaseOutcome.TIMEOUT
    assert client.posted == []


def test_release_mints_a_system_write_ticket_for_the_curator_audience(monkeypatch):
    minted = []
    client = FakeClient(FakeResponse(200, {"status": "released", "freed_bytes": 0}))
    transport = _make_transport(
        "http://curator:8094", {CURATOR_AUDIENCE: 0}, client, monkeypatch, minted=minted
    )

    asyncio.run(transport.request_release(CURATOR_AUDIENCE, 0, 5.0))

    assert minted == [(CURATOR_AUDIENCE, RELEASE_SCOPE)]


# ── the e-stop ──────────────────────────────────────────────────


def test_estop_sets_force(monkeypatch):
    client = FakeClient(
        FakeResponse(200, {"status": "released", "freed_bytes": 6 * GiB, "cancelled_job_ids": ["a"]})
    )
    transport = _make_transport("http://curator:8094", {CURATOR_AUDIENCE: 8 * GiB}, client, monkeypatch)

    resp = asyncio.run(transport.request_release(CURATOR_AUDIENCE, 0, 5.0, force=True))

    assert client.posted[0]["json"]["force"] is True
    assert resp.outcome == ReleaseOutcome.CONFIRMED


def test_confirmed_estop_resolves_held_to_zero_not_pre_minus_freed(monkeypatch):
    """A confirmed e-stop asserts every pipeline was terminated, so held is 0
    regardless of how stale our idea of `pre` was. Deriving from a stale pre
    would strand a phantom hold only an operator reconcile could clear — the
    opposite of what an e-stop is for."""
    # Registry thinks 20 GiB is held; curator reports freeing only 6 GiB
    # (the rest was never really ours / the reading was stale).
    client = FakeClient(FakeResponse(200, {"status": "released", "freed_bytes": 6 * GiB}))
    transport = _make_transport("http://curator:8094", {CURATOR_AUDIENCE: 20 * GiB}, client, monkeypatch)

    resp = asyncio.run(transport.request_release(CURATOR_AUDIENCE, 0, 5.0, force=True))

    assert resp.outcome == ReleaseOutcome.CONFIRMED
    assert resp.new_held_bytes == 0


def test_confirmed_estop_is_zero_even_with_no_pre_call_held(monkeypatch):
    client = FakeClient(FakeResponse(200, {"status": "released", "freed_bytes": 0}))
    transport = _make_transport("http://curator:8094", {}, client, monkeypatch)

    resp = asyncio.run(transport.request_release(CURATOR_AUDIENCE, 0, 5.0, force=True))

    assert resp.new_held_bytes == 0


# ── dispatcher routing ──────────────────────────────────────────


class _RecordingTransport:
    def __init__(self, name):
        self.name = name
        self.release_calls = []

    async def request_release(
        self, consumer_id, amount_bytes, timeout_seconds, force=False
    ):
        self.release_calls.append((consumer_id, force))
        return SimpleNamespace(outcome=ReleaseOutcome.CONFIRMED, new_held_bytes=0, reason=None)


def test_release_dispatcher_routes_curator_to_curator_not_llamolotl():
    """The dispatcher's else-branch falls through to self.llamolotl, so a
    consumer wired without its own route silently POSTs its release to
    llamolotl's endpoint. Curator goes through the extra map — this is the
    regression test for that mis-route."""
    curator = _RecordingTransport("curator")
    llamolotl = _RecordingTransport("llamolotl")
    dispatcher = ConsumerAwareReleaseTransport(
        speak_transport=_RecordingTransport("speak"),
        llamolotl_transport=llamolotl,
        sketch_transport=_RecordingTransport("sketch"),
        extra_transports={CURATOR_AUDIENCE: curator},
    )

    asyncio.run(dispatcher.request_release(CURATOR_AUDIENCE, GiB, 5.0))

    assert curator.release_calls == [(CURATOR_AUDIENCE, False)]
    assert llamolotl.release_calls == []


def test_release_dispatcher_still_falls_through_to_llamolotl():
    llamolotl = _RecordingTransport("llamolotl")
    dispatcher = ConsumerAwareReleaseTransport(
        speak_transport=_RecordingTransport("speak"),
        llamolotl_transport=llamolotl,
        sketch_transport=_RecordingTransport("sketch"),
        extra_transports={CURATOR_AUDIENCE: _RecordingTransport("curator")},
    )

    asyncio.run(dispatcher.request_release("self.llamolotl", GiB, 5.0))

    assert llamolotl.release_calls == [("self.llamolotl", False)]


def test_dispatcher_passes_force_through_to_curator():
    """The system-wide e-stop reaches curator by setting ``force`` on this very
    dispatcher. If the flag were dropped in routing, the button would send
    self.curator a polite ask that a running pipeline is entitled to refuse —
    an e-stop that a consumer can decline is not one."""
    curator = _RecordingTransport("curator")
    dispatcher = ConsumerAwareReleaseTransport(
        speak_transport=_RecordingTransport("speak"),
        llamolotl_transport=_RecordingTransport("llamolotl"),
        sketch_transport=_RecordingTransport("sketch"),
        extra_transports={CURATOR_AUDIENCE: curator},
    )

    asyncio.run(dispatcher.request_release(CURATOR_AUDIENCE, 0, 5.0, force=True))

    assert curator.release_calls == [(CURATOR_AUDIENCE, True)]
