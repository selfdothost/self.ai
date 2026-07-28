"""R2 release-request protocol unit tests — covers AC1–AC5.

GPU-less and network-less: each test drives a temp file-backed SQLite DB with
only the vram_consumer table (patching the vram_leases module's get_db) and a
FAKE release transport injected into the broker. No live consumer, no HTTP —
the fake returns confirmed/denied/timeout on demand per case.

Async code is driven with ``asyncio.run(...)`` inside sync tests, matching
``test_gpu_queue.py`` (the repo registers no pytest-asyncio marker and runs
under --strict-markers).
"""

import asyncio
import time
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import selfai_ui.models.vram_leases as vram_leases
from selfai_ui.models.vram_leases import (
    LEASE_STATE_STALE,
    LEASE_STATE_STEADY,
    VramConsumer,
    VramConsumerRegisterForm,
    VramLeases,
)
from selfai_ui.utils.vram_broker import (
    ReleaseOutcome,
    ReleaseResponse,
    VramBrokerImpl,
)

CARD = 24 * 1024**3
GiB = 1024**3


# ---------------------------------------------------------------------------
# Temp-SQLite fixture (mirrors test_vram_registry.py) + broker helper
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
    db_file = tmp_path / "vram_release_test.db"
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
# Fake transports (no network, no live consumer)
# ---------------------------------------------------------------------------


class FakeTransport:
    """Records every call and returns a preprogrammed response.

    Pass a single ``response`` (reused) or a ``responses`` list (popped in
    order). An optional ``delay`` sleeps before answering — used to exercise
    the broker's per-call timeout bound.
    """

    def __init__(self, response=None, responses=None, delay=0.0):
        self.calls = []
        self._response = response
        self._responses = list(responses) if responses is not None else None
        self.delay = delay

    async def request_release(self, consumer_id, amount_bytes, timeout_seconds):
        self.calls.append((consumer_id, amount_bytes, timeout_seconds))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self._responses is not None:
            return self._responses.pop(0)
        return self._response


class ConcurrencyProbeTransport:
    """Tracks how many release calls are in flight at once. If the broker's
    per-consumer lock works, ``max_active`` never exceeds 1."""

    def __init__(self, hold=0.05):
        self.active = 0
        self.max_active = 0
        self.hold = hold

    async def request_release(self, consumer_id, amount_bytes, timeout_seconds):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.hold)
        finally:
            self.active -= 1
        return ReleaseResponse(outcome=ReleaseOutcome.CONFIRMED, new_held_bytes=0)


# ---------------------------------------------------------------------------
# AC1 — issue release-request to a specific consumer with the target amount
# ---------------------------------------------------------------------------


def test_ac1_release_request_targets_named_consumer_and_amount(vram_db):
    _register("self.llamolotl", held=10 * GiB)
    fake = FakeTransport(response=ReleaseResponse(outcome=ReleaseOutcome.CONFIRMED, new_held_bytes=6 * GiB))
    broker = VramBrokerImpl(transport=fake)

    outcome = asyncio.run(broker.request_release("self.llamolotl", 4 * GiB, timeout_seconds=1.0))

    assert outcome == ReleaseOutcome.CONFIRMED
    assert len(fake.calls) == 1
    consumer_id, amount, timeout = fake.calls[0]
    assert consumer_id == "self.llamolotl"
    assert amount == 4 * GiB
    assert timeout == 1.0


# ---------------------------------------------------------------------------
# AC4 — at most one outstanding request per consumer (lock serializes)
# ---------------------------------------------------------------------------


def test_ac4_concurrent_requests_same_consumer_serialize(vram_db):
    _register("self.llamolotl", held=10 * GiB)
    probe = ConcurrencyProbeTransport(hold=0.05)
    broker = VramBrokerImpl(transport=probe)

    async def _two_concurrent():
        return await asyncio.gather(
            broker.request_release("self.llamolotl", 2 * GiB, timeout_seconds=1.0),
            broker.request_release("self.llamolotl", 2 * GiB, timeout_seconds=1.0),
        )

    outcomes = asyncio.run(_two_concurrent())

    # Both completed, and they never overlapped: the second awaited the first.
    assert outcomes == [ReleaseOutcome.CONFIRMED, ReleaseOutcome.CONFIRMED]
    assert probe.max_active == 1


def test_ac4_different_consumers_do_not_block_each_other(vram_db):
    _register("self.llamolotl", held=10 * GiB)
    _register("self.curator", held=5 * GiB)
    probe = ConcurrencyProbeTransport(hold=0.05)
    broker = VramBrokerImpl(transport=probe)

    async def _two_consumers():
        return await asyncio.gather(
            broker.request_release("self.llamolotl", 2 * GiB, timeout_seconds=1.0),
            broker.request_release("self.curator", 2 * GiB, timeout_seconds=1.0),
        )

    asyncio.run(_two_consumers())

    # Separate locks -> the two ran concurrently.
    assert probe.max_active == 2


# ---------------------------------------------------------------------------
# AC5 — per-call timeout is respected and differs between calls
# ---------------------------------------------------------------------------


def test_ac5_per_call_timeout_is_respected_and_bounds_the_wait(vram_db):
    _register("self.llamolotl", held=10 * GiB)
    # Transport would take 0.5s, but the call passes a 0.05s bound.
    slow = FakeTransport(
        response=ReleaseResponse(outcome=ReleaseOutcome.CONFIRMED, new_held_bytes=0),
        delay=0.5,
    )
    broker = VramBrokerImpl(transport=slow)

    started = time.monotonic()
    outcome = asyncio.run(broker.request_release("self.llamolotl", 4 * GiB, timeout_seconds=0.05))
    elapsed = time.monotonic() - started

    # The per-call bound cut it off well before the transport's 0.5s delay.
    assert outcome == ReleaseOutcome.TIMEOUT
    assert elapsed < 0.4
    # Held untouched by a timeout (see AC3 too).
    assert VramLeases.get("self.llamolotl").held_bytes == 10 * GiB


def test_ac5_timeout_differs_between_calls(vram_db):
    _register("self.llamolotl", held=10 * GiB)
    # Fast transport (0.01s). A tight 0.05s bound still confirms; the point is
    # the per-call timeout value is a real argument that varies between calls.
    fast = FakeTransport(
        response=ReleaseResponse(outcome=ReleaseOutcome.CONFIRMED, new_held_bytes=6 * GiB),
        delay=0.01,
    )
    broker = VramBrokerImpl(transport=fast)

    asyncio.run(broker.request_release("self.llamolotl", 4 * GiB, timeout_seconds=0.05))
    asyncio.run(broker.request_release("self.llamolotl", 4 * GiB, timeout_seconds=2.5))

    # Each call forwarded its own distinct per-call timeout to the transport —
    # not a shared hardcoded constant.
    assert fast.calls[0][2] == 0.05
    assert fast.calls[1][2] == 2.5


# ---------------------------------------------------------------------------
# AC2 — confirmed release records the consumer-reported new held
#        (may be more OR less freed than asked)
# ---------------------------------------------------------------------------


def test_ac2_confirmed_release_records_more_freed_than_asked(vram_db):
    _register("self.llamolotl", held=10 * GiB)
    # Asked for 4 GiB back; consumer evicted a whole model and now holds 2 GiB
    # (freed 8 GiB — MORE than asked).
    fake = FakeTransport(response=ReleaseResponse(outcome=ReleaseOutcome.CONFIRMED, new_held_bytes=2 * GiB))
    broker = VramBrokerImpl(transport=fake)

    outcome = asyncio.run(broker.request_release("self.llamolotl", 4 * GiB, timeout_seconds=1.0))

    assert outcome == ReleaseOutcome.CONFIRMED
    got = VramLeases.get("self.llamolotl")
    assert got.held_bytes == 2 * GiB  # the reported held, not held - asked
    assert got.lease_state == LEASE_STATE_STEADY


def test_ac2_confirmed_release_records_less_freed_than_asked(vram_db):
    _register("self.llamolotl", held=10 * GiB)
    # Asked for 4 GiB back; consumer could only free 2 GiB, now holds 8 GiB
    # (LESS than asked). Still a confirmed release of the reported amount.
    fake = FakeTransport(response=ReleaseResponse(outcome=ReleaseOutcome.CONFIRMED, new_held_bytes=8 * GiB))
    broker = VramBrokerImpl(transport=fake)

    outcome = asyncio.run(broker.request_release("self.llamolotl", 4 * GiB, timeout_seconds=1.0))

    assert outcome == ReleaseOutcome.CONFIRMED
    got = VramLeases.get("self.llamolotl")
    assert got.held_bytes == 8 * GiB
    assert got.lease_state == LEASE_STATE_STEADY


# ---------------------------------------------------------------------------
# AC3 — timeout is distinct from denial; neither is counted as freed
# ---------------------------------------------------------------------------


def test_ac3_timeout_marks_stale_returns_timeout_and_frees_nothing(vram_db):
    _register("self.llamolotl", held=10 * GiB)
    free_before = VramLeases.free_capacity()
    # Transport explicitly signals a timeout (equivalent to no response in time).
    fake = FakeTransport(response=ReleaseResponse(outcome=ReleaseOutcome.TIMEOUT))
    broker = VramBrokerImpl(transport=fake)

    outcome = asyncio.run(broker.request_release("self.llamolotl", 4 * GiB, timeout_seconds=1.0))

    assert outcome == ReleaseOutcome.TIMEOUT
    got = VramLeases.get("self.llamolotl")
    # True state unknown -> stale, distinguishably, held NOT written.
    assert VramLeases.effective_state(got) == LEASE_STATE_STALE
    assert got.held_bytes == 10 * GiB
    # Not counted as freed.
    assert VramLeases.free_capacity() == free_before
    # Excluded from the release-eligible pool (a stale consumer can't be trusted).
    assert "self.llamolotl" not in [h.consumer_id for h in VramLeases.eligible_holders()]


def test_ac3_denied_returns_denied_with_held_unchanged_and_frees_nothing(vram_db):
    _register("self.llamolotl", held=10 * GiB)
    free_before = VramLeases.free_capacity()
    fake = FakeTransport(response=ReleaseResponse(outcome=ReleaseOutcome.DENIED))
    broker = VramBrokerImpl(transport=fake)

    outcome = asyncio.run(broker.request_release("self.llamolotl", 4 * GiB, timeout_seconds=1.0))

    assert outcome == ReleaseOutcome.DENIED
    got = VramLeases.get("self.llamolotl")
    # Explicit refusal: held stays known/unchanged, back to steady (NOT stale).
    assert got.held_bytes == 10 * GiB
    assert got.lease_state == LEASE_STATE_STEADY
    assert VramLeases.effective_state(got) == LEASE_STATE_STEADY
    # Not counted as freed.
    assert VramLeases.free_capacity() == free_before


def test_ac3_timeout_and_denial_are_distinct(vram_db):
    """The same registry state (held=10 GiB) yields two DIFFERENT outcomes and
    two DIFFERENT effective states depending on the consumer's answer — neither
    silently treated as success."""
    _register("timeout.consumer", held=10 * GiB)
    _register("denied.consumer", held=10 * GiB)

    to_broker = VramBrokerImpl(transport=FakeTransport(response=ReleaseResponse(outcome=ReleaseOutcome.TIMEOUT)))
    dn_broker = VramBrokerImpl(transport=FakeTransport(response=ReleaseResponse(outcome=ReleaseOutcome.DENIED)))

    to_outcome = asyncio.run(to_broker.request_release("timeout.consumer", 4 * GiB, timeout_seconds=1.0))
    dn_outcome = asyncio.run(dn_broker.request_release("denied.consumer", 4 * GiB, timeout_seconds=1.0))

    assert to_outcome == ReleaseOutcome.TIMEOUT
    assert dn_outcome == ReleaseOutcome.DENIED
    assert VramLeases.effective_state(VramLeases.get("timeout.consumer")) == LEASE_STATE_STALE
    assert VramLeases.effective_state(VramLeases.get("denied.consumer")) == LEASE_STATE_STEADY


# ---------------------------------------------------------------------------
# Guard — an unconfigured broker never invents a success
# ---------------------------------------------------------------------------


def test_no_transport_raises_rather_than_silently_succeeding(vram_db):
    _register("self.llamolotl", held=10 * GiB)
    broker = VramBrokerImpl()  # no transport (T-016 unlanded)
    with pytest.raises(RuntimeError):
        asyncio.run(broker.request_release("self.llamolotl", 4 * GiB, timeout_seconds=1.0))
    # Held untouched — nothing was freed, no silent success.
    assert VramLeases.get("self.llamolotl").held_bytes == 10 * GiB
