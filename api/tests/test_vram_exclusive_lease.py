"""Decision 6 / R1 exclusive-lease unit tests (T-005).

GPU-less AND cluster-less, mirroring test_vram_force_reap.py: a temp file-backed
SQLite DB holds only the vram_consumer table (patching vram_leases.get_db), and
the broker is driven with a FAKE release transport / reaper. No real Kubernetes,
no HTTP. Covers the R1 exclusive-lease acceptance criteria:

  * acquire clears every other holder (cooperative + reap) then marks exclusive;
  * while an exclusive lease is held, another consumer's grant is DENIED (with the
    holder named);
  * release restores normal grant behavior on the next request;
  * an acquire that cannot clear the card leaves NO exclusive lease (confirmed or
    nothing — no half-held);
  * the ≤1-active-exclusive-holder invariant.

Async code is driven with ``asyncio.run(...)`` inside sync tests, matching the
sibling vram tests (the repo registers no pytest-asyncio marker).
"""

import asyncio
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import selfai_ui.models.vram_leases as vram_leases
from selfai_ui.models.vram_leases import (
    LEASE_MODE_EXCLUSIVE,
    LEASE_MODE_SHARED,
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
# Temp-SQLite fixture + fakes (mirrors test_vram_force_reap.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def vram_db(tmp_path):
    db_file = tmp_path / "vram_exclusive_test.db"
    engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    VramConsumer.__table__.create(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def _get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    original = vram_leases.get_db
    vram_leases.get_db = _get_db
    yield
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


class MappedTransport:
    """Scripted cooperative release responses keyed by consumer_id."""

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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_acquire_clears_holders_then_marks_exclusive(vram_db):
    _register("trainer", held=0)
    _register("ghost", held=10 * GiB, priority=0)
    transport = MappedTransport(responses={"ghost": _confirmed(0)})
    broker = VramBrokerImpl(transport=transport)

    result = asyncio.run(broker.acquire_exclusive("trainer"))

    assert result.acquired is True
    # The other holder was cooperatively cleared to 0 ...
    assert VramLeases.get("ghost").held_bytes == 0
    # ... and the acquirer now holds the exclusive lease.
    assert VramLeases.get("trainer").lease_mode == LEASE_MODE_EXCLUSIVE
    assert VramLeases.active_exclusive_holder().consumer_id == "trainer"


def test_grant_denied_while_exclusive_held(vram_db):
    _register("trainer", held=0)
    _register("other", held=0, priority=1)
    # Trainer holds the card exclusively (no other holders to clear).
    assert VramLeases.set_exclusive("trainer", True) is True

    result = asyncio.run(VramBrokerImpl(transport=MappedTransport()).request_lease("other", 4 * GiB))

    assert isinstance(result, LeaseDenied)
    assert result.exclusive_holder == "trainer"
    assert result.freeable_bytes == 0
    # The denied requester never held anything.
    assert VramLeases.get("other").held_bytes == 0


def test_release_exclusive_restores_normal_grants(vram_db):
    _register("trainer", held=0)
    _register("other", held=0, priority=1)
    assert VramLeases.set_exclusive("trainer", True) is True
    broker = VramBrokerImpl(transport=MappedTransport())

    # Denied while held ...
    denied = asyncio.run(broker.request_lease("other", 4 * GiB))
    assert isinstance(denied, LeaseDenied)

    # ... release, and the very next request is granted from free capacity.
    released = asyncio.run(broker.release_exclusive("trainer"))
    assert released is True
    assert VramLeases.get("trainer").lease_mode == LEASE_MODE_SHARED

    granted = asyncio.run(broker.request_lease("other", 4 * GiB))
    assert isinstance(granted, LeaseGranted)
    assert VramLeases.get("other").held_bytes == 4 * GiB


def test_acquire_fails_when_holder_cannot_be_cleared(vram_db):
    _register("trainer", held=0)
    _register("stubborn", held=10 * GiB, priority=0)
    # The holder DENIES release and there is no reaper — the card can't be cleared.
    transport = MappedTransport(responses={"stubborn": _DENIED})
    broker = VramBrokerImpl(transport=transport)  # reaper defaults to None

    result = asyncio.run(broker.acquire_exclusive("trainer"))

    assert result.acquired is False
    # No half-held exclusive lease: trainer stays shared, the holder is untouched.
    assert VramLeases.get("trainer").lease_mode == LEASE_MODE_SHARED
    assert VramLeases.active_exclusive_holder() is None
    assert VramLeases.get("stubborn").held_bytes == 10 * GiB


def test_at_most_one_active_exclusive_holder(vram_db):
    _register("a", held=0)
    _register("b", held=0, priority=1)

    assert VramLeases.set_exclusive("a", True) is True
    # A second exclusive holder is refused by the invariant ...
    assert VramLeases.set_exclusive("b", True) is False
    # ... and "a" remains the sole exclusive holder.
    assert VramLeases.active_exclusive_holder().consumer_id == "a"
    assert VramLeases.get("b").lease_mode == LEASE_MODE_SHARED
