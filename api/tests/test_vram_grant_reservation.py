"""Grant reservations survive the poller (self.ai#76).

`held_bytes` used to carry two meanings at once: `record_grant` wrote a PROMISE
into it, and the R6 poller's `heartbeat` overwrote the same column with a
MEASUREMENT. A grant issued to a consumer that had not finished allocating was
therefore erased on the next poll cycle, and the next grant handed out the very
same VRAM — the over-grant the broker exists to prevent, arriving through the
broker's own bookkeeping.

The first test here is the bug, written as a sequence: grant -> poll -> grant.
Before the split it ends with 32 GiB granted on a 24 GiB card.

Same GPU-less, temp-SQLite posture as test_vram_registry.py. Async cases use
`asyncio.run(...)` from a sync test — this repo registers no pytest-asyncio and
runs `--strict-markers`.
"""

import asyncio
import time
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import selfai_ui.models.vram_leases as vram_leases
from selfai_ui.models.vram_leases import (
    RESERVATION_TTL_SECONDS,
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


class _NoopTransport:
    """No holder is ever asked to release in these tests; a grant either fits in
    free capacity or is denied."""

    async def request_release(self, consumer_id, amount_bytes, timeout_seconds):
        return ReleaseResponse(outcome=ReleaseOutcome.DENIED)


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
    db_file = tmp_path / "vram_reservation_test.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    VramConsumer.__table__.create(engine)
    Session, get_db = _make_session_get_db(engine)

    original = vram_leases.get_db
    vram_leases.get_db = get_db
    yield {"engine": engine, "Session": Session}
    vram_leases.get_db = original
    engine.dispose()


def _register(consumer_id, held=0, priority=0, capacity=CARD):
    return VramLeases.register(
        VramConsumerRegisterForm(
            consumer_id=consumer_id,
            total_capacity_bytes=capacity,
            held_bytes=held,
            priority=priority,
        )
    )


def _age_reservation(vram_db, consumer_id, age_seconds):
    db = vram_db["Session"]()
    row = db.query(VramConsumer).filter_by(consumer_id=consumer_id).first()
    row.reserved_at = int(time.time()) - age_seconds
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# The bug, as a sequence
# ---------------------------------------------------------------------------


def test_poll_between_two_grants_cannot_double_spend_the_card(vram_db):
    """grant -> poll -> grant, on one 24 GiB card.

    self.sketch is granted 16 GiB and starts loading. Before it has allocated
    anything, a poll cycle reads its real usage (still 0) and heartbeats that in.
    Under the old single-column model that heartbeat ERASED the grant, so the
    second 16 GiB request saw a full card and was granted too — 32 GiB promised
    on a 24 GiB device.
    """
    _register("self.sketch")
    _register("self.speak")
    broker = VramBrokerImpl(transport=_NoopTransport())

    first = asyncio.run(broker.request_lease("self.sketch", 16 * GiB, priority=3))
    assert isinstance(first, LeaseGranted)

    # The poller reads self.sketch mid-load: nothing on the card yet.
    VramLeases.heartbeat("self.sketch", 0)

    # The reservation must survive that measurement.
    assert VramLeases.get("self.sketch").reserved_bytes == 16 * GiB
    assert VramLeases.free_capacity() == CARD - 16 * GiB

    second = asyncio.run(broker.request_lease("self.speak", 16 * GiB, priority=5))
    assert isinstance(second, LeaseDenied), "the card was double-spent"
    assert second.free_bytes == CARD - 16 * GiB


def test_heartbeat_of_a_partial_allocation_does_not_release_the_rest(vram_db):
    """Half-loaded is not released. A measurement BELOW the reservation leaves
    the reservation standing — that window is the whole point of the column."""
    _register("self.sketch")
    broker = VramBrokerImpl(transport=_NoopTransport())
    asyncio.run(broker.request_lease("self.sketch", 16 * GiB, priority=3))

    VramLeases.heartbeat("self.sketch", 6 * GiB)  # mid-load

    row = VramLeases.get("self.sketch")
    assert row.held_bytes == 6 * GiB
    assert row.reserved_bytes == 16 * GiB
    # Effective holding is the reservation, not the partial measurement.
    assert VramLeases.free_capacity() == CARD - 16 * GiB


# ---------------------------------------------------------------------------
# Retirement: the promise gives way to the measurement
# ---------------------------------------------------------------------------


def test_fulfilled_reservation_is_retired_by_the_measurement(vram_db):
    """Once the consumer is observed holding what was reserved, the promise has
    been kept and must stop counting — otherwise it double-counts forever."""
    _register("self.sketch")
    broker = VramBrokerImpl(transport=_NoopTransport())
    asyncio.run(broker.request_lease("self.sketch", 16 * GiB, priority=3))

    VramLeases.heartbeat("self.sketch", 16 * GiB)  # allocation landed

    row = VramLeases.get("self.sketch")
    assert row.held_bytes == 16 * GiB
    assert not row.reserved_bytes
    # Still exactly one 16 GiB hold, not two.
    assert VramLeases.total_held() == 16 * GiB
    assert VramLeases.free_capacity() == CARD - 16 * GiB


def test_measurement_overshooting_the_reservation_also_retires_it(vram_db):
    """A consumer may end up holding MORE than it asked for (allocator slack).
    That is still the promise being kept."""
    _register("self.sketch")
    broker = VramBrokerImpl(transport=_NoopTransport())
    asyncio.run(broker.request_lease("self.sketch", 16 * GiB, priority=3))

    VramLeases.heartbeat("self.sketch", 17 * GiB)

    row = VramLeases.get("self.sketch")
    assert not row.reserved_bytes
    assert VramLeases.total_held() == 17 * GiB


def test_confirmed_release_drops_any_outstanding_reservation(vram_db):
    """A consumer that has just released is not mid-allocation."""
    _register("self.sketch")
    broker = VramBrokerImpl(transport=_NoopTransport())
    asyncio.run(broker.request_lease("self.sketch", 16 * GiB, priority=3))

    VramLeases.record_confirmed_release("self.sketch", 0)

    row = VramLeases.get("self.sketch")
    assert not row.reserved_bytes
    assert VramLeases.total_held() == 0


def test_reconcile_clears_the_reservation_too(vram_db):
    _register("self.sketch")
    broker = VramBrokerImpl(transport=_NoopTransport())
    asyncio.run(broker.request_lease("self.sketch", 16 * GiB, priority=3))

    VramLeases.reconcile("self.sketch")

    assert not VramLeases.get("self.sketch").reserved_bytes
    assert VramLeases.free_capacity() == CARD


# ---------------------------------------------------------------------------
# Expiry: a promise the consumer never took up
# ---------------------------------------------------------------------------


def test_unfulfilled_reservation_expires_and_frees_capacity(vram_db):
    """A consumer granted VRAM that then dies before allocating must not hold
    capacity out of the pool forever — no confirmation is ever coming.

    Deliberately unlike a stale HELD, which is never auto-freed (R1-AC7): a
    stale held is evidence of real VRAM nobody has proven released, while an
    expired reservation is a promise the consumer demonstrably never took up."""
    _register("self.sketch")
    broker = VramBrokerImpl(transport=_NoopTransport())
    asyncio.run(broker.request_lease("self.sketch", 16 * GiB, priority=3))
    assert VramLeases.free_capacity() == CARD - 16 * GiB

    _age_reservation(vram_db, "self.sketch", RESERVATION_TTL_SECONDS + 30)

    assert VramLeases.total_held() == 0
    assert VramLeases.free_capacity() == CARD


def test_reservation_within_ttl_still_counts(vram_db):
    _register("self.sketch")
    broker = VramBrokerImpl(transport=_NoopTransport())
    asyncio.run(broker.request_lease("self.sketch", 16 * GiB, priority=3))

    _age_reservation(vram_db, "self.sketch", RESERVATION_TTL_SECONDS - 30)

    assert VramLeases.total_held() == 16 * GiB


# ---------------------------------------------------------------------------
# Interaction with the self.ai#74 card-occupancy term
# ---------------------------------------------------------------------------


def test_overhead_is_reconciled_against_measured_held_not_reservations(vram_db):
    """#74's unattributed overhead is `card_used - measured_held`. Counting a
    not-yet-allocated reservation there would shrink the overhead by VRAM that
    is not on the card yet, and free capacity would drift UP — the wrong
    direction. It must reconcile against measured held only."""
    _register("self.speak", held=2 * GiB)
    _register("self.sketch")
    broker = VramBrokerImpl(transport=_NoopTransport())
    asyncio.run(broker.request_lease("self.sketch", 8 * GiB, priority=3))

    # Card reports 3 GiB in use: self.speak's 2 GiB plus ~1 GiB of contexts.
    # self.sketch's 8 GiB reservation is NOT on the card yet.
    VramLeases.record_device_occupancy("self.speak", 3 * GiB, CARD)

    assert VramLeases.total_measured_held() == 2 * GiB
    # Overhead measured against MEASURED held: 3 - 2 = 1 GiB.
    assert VramLeases.unattributed_overhead() == 1 * GiB
    # Effective held still carries the reservation: 2 + 8.
    assert VramLeases.total_held() == 10 * GiB
    assert VramLeases.free_capacity() == CARD - 10 * GiB - 1 * GiB


def test_capacity_summary_agrees_with_free_capacity_under_a_reservation(vram_db):
    _register("self.sketch")
    broker = VramBrokerImpl(transport=_NoopTransport())
    asyncio.run(broker.request_lease("self.sketch", 8 * GiB, priority=3))

    summary = VramLeases.capacity_summary()
    assert summary.free_bytes == VramLeases.free_capacity()
    assert summary.total_held_bytes == 8 * GiB


# ---------------------------------------------------------------------------
# The reservation is not a holder
# ---------------------------------------------------------------------------


def test_a_reservation_alone_does_not_make_a_consumer_reclaimable(vram_db):
    """eligible_holders() is the pool R2 asks to RELEASE. A consumer that has
    only a reservation has nothing physical to give back yet, so asking it would
    be pointless — eligibility stays keyed on measured held."""
    _register("self.sketch")
    broker = VramBrokerImpl(transport=_NoopTransport())
    asyncio.run(broker.request_lease("self.sketch", 8 * GiB, priority=3))

    assert [h.consumer_id for h in VramLeases.eligible_holders()] == []


def test_two_grants_before_either_lands_reserve_both(vram_db):
    """Reservations accumulate, matching the old held-increment semantics."""
    _register("self.sketch")
    broker = VramBrokerImpl(transport=_NoopTransport())
    asyncio.run(broker.request_lease("self.sketch", 4 * GiB, priority=3))
    result = asyncio.run(broker.request_lease("self.sketch", 5 * GiB, priority=3))

    assert isinstance(result, LeaseGranted)
    assert VramLeases.get("self.sketch").reserved_bytes == 9 * GiB
    # And the grant reports the effective total, not just the latest slice.
    assert result.held_bytes == 9 * GiB
