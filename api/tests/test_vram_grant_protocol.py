"""R3 lease-grant protocol unit tests — covers AC1–AC7.

GPU-less and network-less, mirroring test_vram_release_protocol.py: each test
drives a temp file-backed SQLite DB with only the vram_consumer table (patching
the vram_leases module's get_db) and injects a FAKE release transport into the
broker. No live consumer, no HTTP — the fake returns confirmed/denied/timeout
per consumer on demand.

The load-bearing property under test is R3-AC4's non-speculative guarantee: a
grant that relies on a release which times out (or is denied) must NOT succeed,
and must leave no phantom hold on the requester. Async code is driven with
``asyncio.run(...)`` inside sync tests, matching test_gpu_queue.py (the repo
registers no pytest-asyncio marker and runs under --strict-markers).
"""

import asyncio
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import selfai_ui.models.vram_leases as vram_leases
from selfai_ui.models.vram_leases import (
    LEASE_STATE_STALE,
    VramConsumer,
    VramConsumerRegisterForm,
    VramLeases,
)
from selfai_ui.utils.vram_broker import (
    LeaseDenied,
    LeaseGranted,
    ReleaseOutcome,
    ReleaseResponse,
    VramBrokerImpl,
)

CARD = 24 * 1024**3
GiB = 1024**3


# ---------------------------------------------------------------------------
# Temp-SQLite fixture (mirrors test_vram_release_protocol.py)
# ---------------------------------------------------------------------------


def _make_session_get_db(engine):
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def _get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    return Session, _get_db


@pytest.fixture
def vram_db(tmp_path):
    """Fresh temp SQLite with only vram_consumer; patches vram_leases.get_db."""
    db_file = tmp_path / "vram_grant_test.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    VramConsumer.__table__.create(engine)
    Session, get_db = _make_session_get_db(engine)

    original = vram_leases.get_db
    vram_leases.get_db = get_db
    yield {"engine": engine, "file": str(db_file), "Session": Session}
    vram_leases.get_db = original
    engine.dispose()


def _register(consumer_id, held, priority=0, capacity=CARD):
    return VramLeases.register(
        VramConsumerRegisterForm(
            consumer_id=consumer_id,
            total_capacity_bytes=capacity,
            held_bytes=held,
            priority=priority,
        )
    )


# ---------------------------------------------------------------------------
# Fake transport — per-consumer scripted release responses + call order
# ---------------------------------------------------------------------------


class MappedTransport:
    """Scripted release responses keyed by consumer_id, recording call order.

    A grant's reclamation loop calls ``request_release`` once per holder; this
    fake answers each with a preprogrammed :class:`ReleaseResponse` and records
    the ``(consumer_id, amount, timeout)`` of every call so a test can assert the
    ask ORDER (ascending priority) and that the free-path issues zero calls.
    """

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    async def request_release(self, consumer_id, amount_bytes, timeout_seconds):
        self.calls.append((consumer_id, amount_bytes, timeout_seconds))
        if consumer_id not in self.responses:
            raise AssertionError(f"unexpected release-request to {consumer_id!r}")
        return self.responses[consumer_id]


def _confirmed(new_held):
    return ReleaseResponse(outcome=ReleaseOutcome.CONFIRMED, new_held_bytes=new_held)


_DENIED = ReleaseResponse(outcome=ReleaseOutcome.DENIED)
_TIMEOUT = ReleaseResponse(outcome=ReleaseOutcome.TIMEOUT)


# ---------------------------------------------------------------------------
# AC1 — a lease request carries requesting consumer, amount, and priority
# ---------------------------------------------------------------------------


def test_ac1_request_carries_consumer_amount_and_priority(vram_db):
    # Registered with priority 7; requests a lease at priority 3 — the REQUEST's
    # priority is what is carried and recorded, distinct from any prior value.
    _register("trainer", held=0, priority=7)
    broker = VramBrokerImpl(transport=MappedTransport())

    result = asyncio.run(broker.request_lease("trainer", 4 * GiB, priority=3))

    assert isinstance(result, LeaseGranted)
    # consumer carried
    assert result.consumer_id == "trainer"
    # amount carried
    assert result.granted_bytes == 4 * GiB
    row = VramLeases.get("trainer")
    assert row.held_bytes == 4 * GiB
    # priority carried — the request's priority (3), not the registration's (7),
    # is recorded on the row so a later grant reclaims it in the right order.
    assert result.priority == 3
    assert row.priority == 3


# ---------------------------------------------------------------------------
# AC2 — a request satisfiable from free capacity issues NO release-request
# ---------------------------------------------------------------------------


def test_ac2_free_path_grants_without_any_release_request(vram_db):
    _register("trainer", held=0)
    # A holder exists and COULD be reclaimed, but free already suffices, so it
    # must not be asked at all.
    _register("self.llamolotl", held=2 * GiB)
    transport = MappedTransport()  # would raise if any release were issued
    broker = VramBrokerImpl(transport=transport)

    result = asyncio.run(broker.request_lease("trainer", 4 * GiB, priority=0))

    assert isinstance(result, LeaseGranted)
    assert result.reclaimed == []
    # The whole point: zero release-requests on the free-path.
    assert transport.calls == []
    # Holder untouched.
    assert VramLeases.get("self.llamolotl").held_bytes == 2 * GiB


# ---------------------------------------------------------------------------
# AC6 — the granted hold is written to the registry BEFORE success returns
# ---------------------------------------------------------------------------


def test_ac6_held_written_before_success_returns(vram_db):
    _register("trainer", held=0)
    broker = VramBrokerImpl(transport=MappedTransport())

    result = asyncio.run(broker.request_lease("trainer", 6 * GiB, priority=0))

    # By the time request_lease has returned success, the hold is already
    # committed to the registry (record_grant runs before the return).
    assert isinstance(result, LeaseGranted)
    assert result.held_bytes == 6 * GiB
    assert VramLeases.get("trainer").held_bytes == 6 * GiB
    # Free capacity dropped by the granted amount.
    assert VramLeases.free_capacity() == CARD - 6 * GiB


# ---------------------------------------------------------------------------
# AC3 — insufficient free asks holders in ascending-priority order
# ---------------------------------------------------------------------------


def test_ac3_insufficient_free_asks_holders_ascending_priority(vram_db):
    _register("trainer", held=0)
    # Deliberately register the HIGHER-priority-number holder first so a naive
    # insertion-order ask would fail this test — only a priority sort passes.
    _register("holder.hi", held=8 * GiB, priority=5)
    _register("holder.lo", held=8 * GiB, priority=1)
    # free = 24 - 16 = 8 GiB; request 18 GiB forces reclamation of both.
    transport = MappedTransport(
        responses={
            "holder.lo": _confirmed(4 * GiB),  # frees 4
            "holder.hi": _confirmed(2 * GiB),  # frees 6
        }
    )
    broker = VramBrokerImpl(transport=transport)

    # Requester priority 100 outranks both holders (1, 5); the #3 priority gate
    # only lets a request reclaim STRICTLY-lower-priority holders.
    result = asyncio.run(broker.request_lease("trainer", 18 * GiB, priority=100))

    assert isinstance(result, LeaseGranted)
    # Lower-priority holder asked FIRST (ascending priority: 1 before 5).
    asked_order = [c[0] for c in transport.calls]
    assert asked_order == ["holder.lo", "holder.hi"]
    # Grant landed only after the confirmed releases made room.
    assert VramLeases.get("trainer").held_bytes == 18 * GiB


# ---------------------------------------------------------------------------
# AC4 — a grant relying on a release that TIMES OUT does not succeed
#        (non-speculative; no phantom hold on the requester)
# ---------------------------------------------------------------------------


def test_ac4_grant_relying_on_timed_out_release_does_not_succeed(vram_db):
    _register("trainer", held=0)
    _register("self.llamolotl", held=10 * GiB, priority=0)
    # free = 14 GiB; request 20 GiB needs the holder to free ~6 GiB, but it
    # times out — its true state is now unknown, nothing is actually freed.
    transport = MappedTransport(responses={"self.llamolotl": _TIMEOUT})
    broker = VramBrokerImpl(transport=transport)

    # Requester priority 100 outranks the holder (0) so it IS asked (then times
    # out) — the #3 gate governs eligibility, not the non-speculative guarantee.
    result = asyncio.run(broker.request_lease("trainer", 20 * GiB, priority=100))

    # Denied — never granted speculatively against a release that didn't land.
    assert isinstance(result, LeaseDenied)
    # No phantom hold: the requester's held was never written.
    assert VramLeases.get("trainer").held_bytes == 0
    # The holder's held is untouched and it is now stale (state unknown).
    holder = VramLeases.get("self.llamolotl")
    assert holder.held_bytes == 10 * GiB
    assert VramLeases.effective_state(holder) == LEASE_STATE_STALE
    # The breakdown records the timeout distinctly, with nothing released.
    assert len(result.holders_asked) == 1
    ha = result.holders_asked[0]
    assert ha.consumer_id == "self.llamolotl"
    assert ha.outcome == ReleaseOutcome.TIMEOUT.value
    assert ha.actually_released == 0


# ---------------------------------------------------------------------------
# AC5 — exhausted-but-insufficient denies with the full structured reason
# ---------------------------------------------------------------------------


def test_ac5_exhausted_denies_with_structured_reason_and_breakdown(vram_db):
    _register("trainer", held=0)
    _register("holder.a", held=4 * GiB, priority=1)
    _register("holder.b", held=4 * GiB, priority=2)
    # free = 24 - 8 = 16 GiB; freeable = 16 + 8 = 24 GiB (>= 22, so NOT an
    # upfront over-capacity denial). But holder.a frees all it has (4) and
    # holder.b refuses, so reclamation reaches only 20 GiB — short of 22.
    transport = MappedTransport(
        responses={
            "holder.a": _confirmed(0),  # frees its whole 4 GiB
            "holder.b": _DENIED,        # refuses
        }
    )
    broker = VramBrokerImpl(transport=transport)

    # Requester priority 100 outranks both holders (1, 2); the #3 gate lets it
    # reclaim them, so freeable is the full free+releasable as this AC intends.
    result = asyncio.run(broker.request_lease("trainer", 22 * GiB, priority=100))

    assert isinstance(result, LeaseDenied)
    # The three named facts (R3-AC5): requested, actually free, freeable.
    assert result.requested_bytes == 22 * GiB
    assert result.free_bytes == 20 * GiB          # 16 + 4 confirmed-freed
    assert result.freeable_bytes == 24 * GiB      # optimistic free+releasable
    # Per-holder breakdown: who was asked, how much they released, the outcome.
    by_id = {h.consumer_id: h for h in result.holders_asked}
    assert set(by_id) == {"holder.a", "holder.b"}
    assert by_id["holder.a"].outcome == ReleaseOutcome.CONFIRMED.value
    assert by_id["holder.a"].actually_released == 4 * GiB
    assert by_id["holder.b"].outcome == ReleaseOutcome.DENIED.value
    assert by_id["holder.b"].actually_released == 0
    # No phantom hold on denial.
    assert VramLeases.get("trainer").held_bytes == 0


# ---------------------------------------------------------------------------
# AC7 — a request beyond total free-plus-releasable is denied at ANY priority
# ---------------------------------------------------------------------------


def test_ac7_over_total_freeable_denied_regardless_of_priority(vram_db):
    _register("trainer", held=0)
    _register("self.llamolotl", held=8 * GiB, priority=0)
    # free = 16 GiB, releasable = 8 GiB, freeable = 24 GiB. A 30 GiB request
    # exceeds even free-plus-releasable — the broker never invents VRAM.
    transport = MappedTransport()  # would raise if anyone were asked to release
    broker = VramBrokerImpl(transport=transport)

    # Denied identically at a low and a very high priority — the over-total denial
    # is priority-independent. Both requester priorities outrank the holder (0) so
    # freeable is the same 24 GiB in each case (the #3 gate makes freeable depend on
    # priority, so we keep both requesters above the holder to isolate THIS AC).
    for priority in (1, 1_000_000):
        result = asyncio.run(broker.request_lease("trainer", 30 * GiB, priority=priority))
        assert isinstance(result, LeaseDenied)
        assert result.requested_bytes == 30 * GiB
        assert result.free_bytes == 16 * GiB
        assert result.freeable_bytes == 24 * GiB
        # Denied UP FRONT — no holder was ever asked to release.
        assert result.holders_asked == []

    assert transport.calls == []
    # No hold ever written to the requester across either attempt.
    assert VramLeases.get("trainer").held_bytes == 0


# ---------------------------------------------------------------------------
# Guard — an unregistered requester is a protocol error, not a silent grant
# ---------------------------------------------------------------------------


def test_unregistered_requester_raises(vram_db):
    broker = VramBrokerImpl(transport=MappedTransport())
    with pytest.raises(ValueError):
        asyncio.run(broker.request_lease("ghost", 4 * GiB, priority=0))


# ---------------------------------------------------------------------------
# #3 priority gate — a grant never reclaims an equal-/higher-priority holder
# ---------------------------------------------------------------------------


def test_priority_gate_low_requester_cannot_reclaim_higher_holder(vram_db):
    # self.sketch (priority 3) wants VRAM the brain (priority 10) is holding.
    # Even though free+brain's held could cover it, the gate forbids a
    # lower-priority requester from reclaiming a higher-priority holder: DENY up
    # front, and the brain is NEVER asked to release.
    _register("self.sketch", held=0, priority=3)
    _register("self.llamolotl", held=20 * GiB, priority=10)
    # free = 24 - 20 = 4 GiB; sketch requests 8 GiB.
    transport = MappedTransport()  # raises if any release is issued
    broker = VramBrokerImpl(transport=transport)

    result = asyncio.run(broker.request_lease("self.sketch", 8 * GiB, priority=3))

    assert isinstance(result, LeaseDenied)
    # The higher-priority holder was never asked and holds exactly what it did.
    assert transport.calls == []
    assert result.holders_asked == []
    assert VramLeases.get("self.llamolotl").held_bytes == 20 * GiB
    # Freeable reflects ONLY free capacity — the brain's held is not reclaimable
    # by this requester, so it is excluded from the freeable ceiling.
    assert result.freeable_bytes == 4 * GiB
    # No phantom hold on the denied requester.
    assert VramLeases.get("self.sketch").held_bytes == 0


def test_priority_gate_reclaims_only_strictly_lower(vram_db):
    # A mid-priority requester (5) may reclaim a strictly-lower holder (3) but
    # NOT a higher one (8): only "lo" is asked, "hi" is never touched.
    _register("mid", held=0, priority=5)
    _register("lo", held=8 * GiB, priority=3)
    _register("hi", held=8 * GiB, priority=8)
    # free = 24 - 16 = 8 GiB; request 12 GiB — needs 4 GiB reclaimed, reachable
    # only from "lo" (hi is above the requester, out of reach).
    transport = MappedTransport(responses={"lo": _confirmed(0)})  # lo frees its 8
    broker = VramBrokerImpl(transport=transport)

    result = asyncio.run(broker.request_lease("mid", 12 * GiB, priority=5))

    assert isinstance(result, LeaseGranted)
    # Only the strictly-lower holder was asked; the higher one never was.
    assert [c[0] for c in transport.calls] == ["lo"]
    assert VramLeases.get("hi").held_bytes == 8 * GiB  # untouched
    assert VramLeases.get("mid").held_bytes == 12 * GiB


def test_priority_gate_equal_priority_peer_not_reclaimed(vram_db):
    # Peers at the SAME priority never reclaim each other (strict <): the request
    # is denied rather than evicting an equal-priority holder.
    _register("a", held=0, priority=5)
    _register("b", held=10 * GiB, priority=5)
    # free = 14 GiB; "a" requests 20 GiB — would need "b"'s VRAM, but b is a peer.
    transport = MappedTransport()  # raises if b is asked
    broker = VramBrokerImpl(transport=transport)

    result = asyncio.run(broker.request_lease("a", 20 * GiB, priority=5))

    assert isinstance(result, LeaseDenied)
    assert transport.calls == []  # peer never asked
    assert result.holders_asked == []
    assert VramLeases.get("b").held_bytes == 10 * GiB
    # Only free capacity is grantable; the peer's held is not reclaimable.
    assert result.freeable_bytes == 14 * GiB
