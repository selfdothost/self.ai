"""Card-level device-occupancy accounting (self.ai#74).

`total_held()` sums a PER-CONSUMER figure. That is only sound if every consumer
measures the same disjoint thing about itself — and even then it cannot see CUDA
contexts or processes that are not lease consumers. These tests pin the
correction: a separate card-level reading, a derived unattributed-overhead term,
and a `free_capacity()` that subtracts it WITHOUT losing the immediate feedback
R3's reclamation loop depends on.

Same GPU-less, temp-SQLite posture as test_vram_registry.py. Async cases use
`asyncio.run(...)` from a sync test — this repo registers no pytest-asyncio and
runs under `--strict-markers`, so `@pytest.mark.asyncio` would error.
"""

import asyncio
import time
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import selfai_ui.models.vram_leases as vram_leases
from selfai_ui.models.vram_leases import (
    STALE_THRESHOLD_SECONDS,
    VramConsumer,
    VramConsumerRegisterForm,
    VramLeases,
)
from selfai_ui.utils.vram_poller import _poll_once
from selfai_ui.utils.vram_state_source import parse_device_occupancy

CARD = 24 * 1024**3
GB = 1024**3


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
    db_file = tmp_path / "vram_device_occupancy_test.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    VramConsumer.__table__.create(engine)
    Session, get_db = _make_session_get_db(engine)

    original = vram_leases.get_db
    vram_leases.get_db = get_db
    yield {"engine": engine, "Session": Session}
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


def _age_device_reading(vram_db, consumer_id, age_seconds):
    """Backdate a consumer's device_reported_at to simulate an old card read."""
    db = vram_db["Session"]()
    row = db.query(VramConsumer).filter_by(consumer_id=consumer_id).first()
    row.device_reported_at = int(time.time()) - age_seconds
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# The fallback: with no card reading, nothing changes
# ---------------------------------------------------------------------------


def test_no_device_reading_falls_back_to_pure_ledger_arithmetic(vram_db):
    """A consumer that never reports card occupancy must leave free_capacity()
    exactly as it was before self.ai#74 — the overhead is never invented."""
    _register("self.llamolotl", held=8 * GB)
    _register("self.speak", held=2 * GB)

    assert VramLeases.device_occupancy() is None
    assert VramLeases.unattributed_overhead() == 0
    assert VramLeases.free_capacity() == CARD - 10 * GB


# ---------------------------------------------------------------------------
# The correction: overhead the ledger cannot see is subtracted
# ---------------------------------------------------------------------------


def test_unattributed_overhead_is_card_used_minus_ledger_held(vram_db):
    """CUDA contexts + non-consumer processes: the card is using more than the
    sum of what consumers claim, and the difference must reduce free."""
    _register("self.llamolotl", held=8 * GB)
    _register("self.speak", held=2 * GB)

    # Card says 12 GiB in use; consumers only account for 10 GiB.
    VramLeases.record_device_occupancy("self.llamolotl", 12 * GB, CARD)

    occ = VramLeases.device_occupancy()
    assert occ is not None
    assert occ.used_bytes == 12 * GB
    assert occ.reported_by == "self.llamolotl"
    assert occ.unattributed_bytes == 2 * GB
    assert VramLeases.unattributed_overhead() == 2 * GB
    # Free is the card total minus BOTH the ledger held and the overhead.
    assert VramLeases.free_capacity() == CARD - 10 * GB - 2 * GB


def test_card_reading_below_ledger_never_produces_negative_overhead(vram_db):
    """A consumer's held can legitimately exceed the instantaneous card read
    (a reservation recorded before allocation). That must clamp to zero
    overhead, never a negative that would INFLATE free capacity."""
    _register("self.llamolotl", held=8 * GB)
    VramLeases.record_device_occupancy("self.llamolotl", 3 * GB, CARD)

    assert VramLeases.unattributed_overhead() == 0
    assert VramLeases.free_capacity() == CARD - 8 * GB


def test_device_occupancy_is_never_summed_into_total_held(vram_db):
    """The whole point of #74: a card-level figure must not land in held_bytes.
    record_device_occupancy is not a held writer."""
    _register("self.llamolotl", held=8 * GB)
    VramLeases.record_device_occupancy("self.llamolotl", 20 * GB, CARD)

    assert VramLeases.total_held() == 8 * GB
    assert VramLeases.get("self.llamolotl").held_bytes == 8 * GB


def test_device_reading_does_not_disturb_lease_state_or_liveness(vram_db):
    """It is a different quantity on a different clock — it must not count as a
    heartbeat, or a dead consumer could be kept alive by a card read."""
    _register("self.llamolotl", held=8 * GB)
    before = VramLeases.get("self.llamolotl")

    VramLeases.record_device_occupancy("self.llamolotl", 12 * GB, CARD)

    after = VramLeases.get("self.llamolotl")
    assert after.last_reported_at == before.last_reported_at
    assert after.lease_state == before.lease_state


# ---------------------------------------------------------------------------
# Freshness — an old card read must not keep subtracting
# ---------------------------------------------------------------------------


def test_stale_device_reading_is_ignored(vram_db):
    """An overhead figure is only safe to subtract while it is current."""
    _register("self.llamolotl", held=8 * GB)
    VramLeases.record_device_occupancy("self.llamolotl", 12 * GB, CARD)
    _age_device_reading(vram_db, "self.llamolotl", STALE_THRESHOLD_SECONDS + 60)

    assert VramLeases.device_occupancy() is None
    assert VramLeases.free_capacity() == CARD - 8 * GB


def test_freshest_reading_wins_when_several_consumers_report(vram_db):
    """All consumers measure the SAME physical card, so the newest read is the
    best one — not a sum, and not the largest."""
    _register("self.llamolotl", held=4 * GB)
    _register("self.speak", held=1 * GB)

    VramLeases.record_device_occupancy("self.llamolotl", 20 * GB, CARD)
    _age_device_reading(vram_db, "self.llamolotl", 30)
    VramLeases.record_device_occupancy("self.speak", 6 * GB, CARD)

    occ = VramLeases.device_occupancy()
    assert occ.reported_by == "self.speak"
    assert occ.used_bytes == 6 * GB


# ---------------------------------------------------------------------------
# The load-bearing property: a confirmed release must free capacity AT ONCE
# ---------------------------------------------------------------------------


def test_confirmed_release_raises_free_capacity_immediately(vram_db):
    """R3's reclamation loop re-reads free_capacity() each round and stops when
    it covers the request. If free were driven by the card reading alone it
    would not move until the next poll and the loop would stall for a whole
    interval, asking every holder in turn for VRAM that was already returned."""
    _register("self.llamolotl", held=10 * GB)
    VramLeases.record_device_occupancy("self.llamolotl", 12 * GB, CARD)
    before = VramLeases.free_capacity()

    # A confirmed release lands in the ledger; no new card reading yet.
    VramLeases.record_confirmed_release("self.llamolotl", 4 * GB)

    after = VramLeases.free_capacity()
    assert after == before + 6 * GB
    # The overhead term is untouched — it only moves on a poll.
    assert VramLeases.unattributed_overhead() == 2 * GB


def test_capacity_summary_reports_the_same_free_as_free_capacity(vram_db):
    """The operator view and the grant decision must never disagree about how
    much of the card is grantable."""
    _register("self.llamolotl", held=8 * GB)
    VramLeases.record_device_occupancy("self.llamolotl", 12 * GB, CARD)

    summary = VramLeases.capacity_summary()
    assert summary.free_bytes == VramLeases.free_capacity()
    # One consumer holding 8 GiB against a 12 GiB card reading -> 4 GiB the
    # ledger cannot account for.
    assert summary.unattributed_bytes == 4 * GB
    assert summary.device_occupancy.reported_by == "self.llamolotl"


def test_record_device_occupancy_is_a_noop_for_unknown_consumer(vram_db):
    VramLeases.record_device_occupancy("self.nobody", 12 * GB, CARD)
    assert VramLeases.device_occupancy() is None


# ---------------------------------------------------------------------------
# Wire parsing
# ---------------------------------------------------------------------------


def test_parse_device_occupancy_reads_used_and_total():
    assert parse_device_occupancy(
        {"device_used_bytes": 12 * GB, "total_capacity_bytes": CARD}
    ) == (12 * GB, CARD)


def test_parse_device_occupancy_without_used_yields_nothing():
    """A card total on its own says nothing about occupancy — recording it would
    imply a reading that was never taken."""
    assert parse_device_occupancy({"total_capacity_bytes": CARD}) == (None, None)


def test_parse_device_occupancy_rejects_bool_and_non_dict():
    assert parse_device_occupancy({"device_used_bytes": True}) == (None, None)
    assert parse_device_occupancy(["not", "a", "dict"]) == (None, None)


def test_parse_device_occupancy_tolerates_a_consumer_that_omits_the_fields():
    """The rollout order: core accepts the field before any consumer sends it."""
    assert parse_device_occupancy({"held_vram_bytes": 4 * GB, "status": "ok"}) == (
        None,
        None,
    )


# ---------------------------------------------------------------------------
# Poller relay
# ---------------------------------------------------------------------------


class _FullSource:
    """A source on the newest contract (held + loaded model + card occupancy)."""

    def __init__(self, held, device_used, device_total=CARD):
        self._held = held
        self._device_used = device_used
        self._device_total = device_total

    async def read_full(self, consumer_id):
        return (self._held, None, self._device_used, self._device_total)


class _HeldOnlySource:
    """A pre-#74 source exposing only read_held_bytes — must keep working."""

    def __init__(self, held):
        self._held = held

    async def read_held_bytes(self, consumer_id):
        return self._held


def test_poller_relays_card_occupancy_alongside_held(vram_db):
    _register("self.llamolotl", held=0)
    asyncio.run(
        _poll_once({"self.llamolotl": _FullSource(8 * GB, 12 * GB)}, registry=VramLeases)
    )

    assert VramLeases.get("self.llamolotl").held_bytes == 8 * GB
    occ = VramLeases.device_occupancy()
    assert occ.used_bytes == 12 * GB
    # Derived against THIS cycle's held (8 GiB), not the previous 0 — the poller
    # relays the heartbeat before the card reading for exactly this reason.
    assert occ.unattributed_bytes == 4 * GB


def test_poller_still_works_for_a_source_with_only_read_held_bytes(vram_db):
    _register("self.speak", held=0)
    asyncio.run(_poll_once({"self.speak": _HeldOnlySource(2 * GB)}, registry=VramLeases))

    assert VramLeases.get("self.speak").held_bytes == 2 * GB
    assert VramLeases.device_occupancy() is None


def test_poller_skips_the_card_reading_when_the_consumer_omits_it(vram_db):
    _register("self.speak", held=0)
    asyncio.run(
        _poll_once({"self.speak": _FullSource(2 * GB, None, None)}, registry=VramLeases)
    )

    assert VramLeases.get("self.speak").held_bytes == 2 * GB
    assert VramLeases.device_occupancy() is None
