"""R6 consumer VRAM state poller unit tests — covers AC1–AC6.

GPU-less and network-less: each test drives a temp file-backed SQLite DB with
only the vram_consumer table (patching the vram_leases module's get_db, exactly
like test_vram_release_protocol.py) and a FAKE state source. No live consumer,
no HTTP — the fake returns a held value / None / raises on demand per case.

The poller's cycle is tested through its factored-out ``_poll_once`` helper so a
single cycle can be exercised WITHOUT racing ``asyncio.sleep`` (the ``while
True`` loop's outer resilience is tested separately, in the AC1 loop-survival
case, by breaking out of the sleep).

A ``RecordingRegistry`` wraps the real ``VramLeases`` (so the held write is the
genuine R1 ``heartbeat()`` entry point against the real temp DB) while counting
``heartbeat``/``mark_stale`` calls — that is what lets the tests assert EXACTLY
one heartbeat (AC2) and ZERO ``mark_stale`` calls (AC4) directly.

Async code is driven with ``asyncio.run(...)`` inside sync tests, matching
test_vram_release_protocol.py (the repo registers no pytest-asyncio marker and
runs under --strict-markers).
"""

import asyncio
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import selfai_ui.models.vram_leases as vram_leases
import selfai_ui.utils.vram_poller as poller_mod
from selfai_ui.models.vram_leases import (
    LEASE_STATE_STEADY,
    VramConsumer,
    VramConsumerRegisterForm,
    VramLeases,
)
from selfai_ui.utils.vram_poller import (
    VRAM_POLL_INTERVAL_SECONDS,
    _poll_once,
    process_vram_poll_loop,
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
    """Fresh temp SQLite with only vram_consumer; patches vram_leases.get_db.

    ``VramConsumer.__table__.create`` builds the CURRENT table shape, so the
    R5 k8s pod-identity columns come along for free — but this suite never
    touches them (R6 is capacity/held only)."""
    db_file = tmp_path / "vram_poller_test.db"
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
# Fakes: state sources (no network) + a recording registry wrapper
# ---------------------------------------------------------------------------


class FakeSource:
    """A state source whose ``read_held_bytes`` returns a fixed value, returns
    ``None``, or raises — with an optional ``delay`` to exercise concurrency.
    Records every consumer_id it was asked to read."""

    def __init__(self, held=None, raises=None, delay=0.0):
        self._held = held
        self._raises = raises
        self.delay = delay
        self.reads = []

    async def read_held_bytes(self, consumer_id):
        self.reads.append(consumer_id)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self._raises is not None:
            raise self._raises
        return self._held


class RecordingRegistry:
    """Delegates get_all/heartbeat/mark_stale to the REAL ``VramLeases`` (so the
    held write is the genuine R1 entry point against the temp DB) while counting
    calls. ``mark_stale`` is exposed only so the tests can PROVE the poller never
    calls it (R6-AC4) — the poller has no reason to reach for it."""

    def __init__(self, inner=VramLeases):
        self._inner = inner
        self.heartbeat_calls = []
        self.mark_stale_calls = []

    def get_all(self):
        return self._inner.get_all()

    def heartbeat(self, consumer_id, held_bytes):
        self.heartbeat_calls.append((consumer_id, held_bytes))
        return self._inner.heartbeat(consumer_id, held_bytes)

    def mark_stale(self, consumer_id):
        self.mark_stale_calls.append(consumer_id)
        return self._inner.mark_stale(consumer_id)


# ---------------------------------------------------------------------------
# AC2 — a successful poll relays HELD via the EXISTING heartbeat(); capacity
#        is NOT touched (only held_bytes is relayed)
# ---------------------------------------------------------------------------


def test_ac2_successful_poll_heartbeats_held_only_capacity_untouched(vram_db):
    _register("self.llamolotl", held=4 * GiB, capacity=CARD)
    source = FakeSource(held=9 * GiB)
    registry = RecordingRegistry()

    asyncio.run(_poll_once({"self.llamolotl": source}, registry=registry))

    # Exactly one heartbeat, carrying the consumer_id + the self-reported held
    # ONLY (heartbeat's signature is held_bytes-only — capacity is never a param).
    assert registry.heartbeat_calls == [("self.llamolotl", 9 * GiB)]
    got = VramLeases.get("self.llamolotl")
    assert got.held_bytes == 9 * GiB  # relayed self-report landed
    assert got.total_capacity_bytes == CARD  # capacity fixed at register(), untouched
    # The poller never marks stale on a SUCCESSFUL read either (R6-AC4).
    assert registry.mark_stale_calls == []


def test_ac2_relay_uses_same_entry_point_as_a_direct_self_report(vram_db):
    """The value the poller relays is written by the SAME heartbeat() a
    consumer's own direct self-report uses — a poll is indistinguishable in the
    registry from the consumer heartbeating itself."""
    _register("self.llamolotl", held=4 * GiB)
    # A direct self-report and a poller relay of the same figure produce the
    # same registry state — because it is literally the same write path.
    VramLeases.heartbeat("self.llamolotl", 7 * GiB)
    direct = VramLeases.get("self.llamolotl").held_bytes

    _register("self.llamolotl", held=4 * GiB)  # reset held via idempotent re-register
    asyncio.run(_poll_once({"self.llamolotl": FakeSource(held=7 * GiB)}, registry=RecordingRegistry()))
    polled = VramLeases.get("self.llamolotl").held_bytes

    assert direct == polled == 7 * GiB


# ---------------------------------------------------------------------------
# AC3 — a registered consumer absent from the source map is skipped
# ---------------------------------------------------------------------------


def test_ac3_consumer_absent_from_source_map_is_skipped(vram_db):
    _register("self.llamolotl", held=4 * GiB)
    _register("self.future-consumer", held=2 * GiB)  # registered, no state source
    source = FakeSource(held=9 * GiB)
    registry = RecordingRegistry()

    # Only self.llamolotl has a configured state source.
    asyncio.run(_poll_once({"self.llamolotl": source}, registry=registry))

    # The mapped consumer was heartbeated; the unmapped one was skipped clean —
    # no heartbeat, no error, and its held is left exactly as registered.
    assert registry.heartbeat_calls == [("self.llamolotl", 9 * GiB)]
    assert VramLeases.get("self.future-consumer").held_bytes == 2 * GiB
    assert registry.mark_stale_calls == []


def test_ac3_empty_source_map_polls_nobody_without_error(vram_db):
    """The graceful unconfigured case (T-010's empty map): the cycle runs but
    touches no consumer."""
    _register("self.llamolotl", held=4 * GiB)
    registry = RecordingRegistry()

    asyncio.run(_poll_once({}, registry=registry))

    assert registry.heartbeat_calls == []
    assert VramLeases.get("self.llamolotl").held_bytes == 4 * GiB


# ---------------------------------------------------------------------------
# AC4 — a failed poll (None or raising) does NOT heartbeat and does NOT
#        mark_stale; STALE_THRESHOLD_SECONDS stays the sole detector
# ---------------------------------------------------------------------------


def test_ac4_none_read_does_not_heartbeat_or_mark_stale(vram_db):
    _register("self.llamolotl", held=4 * GiB)
    before = VramLeases.get("self.llamolotl")
    registry = RecordingRegistry()

    # Source cannot read this cycle -> returns None (unreachable/malformed).
    asyncio.run(_poll_once({"self.llamolotl": FakeSource(held=None)}, registry=registry))

    assert registry.heartbeat_calls == []  # not heartbeated this cycle
    assert registry.mark_stale_calls == []  # poller NEVER marks stale (R6-AC4)
    after = VramLeases.get("self.llamolotl")
    # Stored state entirely untouched by the poller — it only ages toward stale
    # via last_reported_at against STALE_THRESHOLD_SECONDS, the sole detector.
    assert after.held_bytes == before.held_bytes
    assert after.lease_state == before.lease_state == LEASE_STATE_STEADY
    assert after.last_reported_at == before.last_reported_at


def test_ac4_raising_read_does_not_heartbeat_or_mark_stale(vram_db):
    _register("self.llamolotl", held=4 * GiB)
    before = VramLeases.get("self.llamolotl")
    registry = RecordingRegistry()

    # Even a source that RAISES (read_held_bytes is meant to swallow and return
    # None, but if it ever raised) must not heartbeat, must not mark stale, and
    # must not propagate (gather isolates it — see AC1/AC5).
    asyncio.run(
        _poll_once({"self.llamolotl": FakeSource(raises=RuntimeError("boom"))}, registry=registry)
    )

    assert registry.heartbeat_calls == []
    assert registry.mark_stale_calls == []
    after = VramLeases.get("self.llamolotl")
    assert after.held_bytes == before.held_bytes
    assert after.last_reported_at == before.last_reported_at


# ---------------------------------------------------------------------------
# AC5 — one consumer's slow/failed poll does not block the others; the fast
#        one's heartbeat still lands (concurrent isolation)
# ---------------------------------------------------------------------------


def test_ac5_slow_raising_source_does_not_block_fast_sources_heartbeat(vram_db):
    _register("fast.consumer", held=1 * GiB)
    _register("slow.consumer", held=1 * GiB)
    fast = FakeSource(held=8 * GiB)  # answers immediately
    slow = FakeSource(raises=RuntimeError("boom"), delay=0.05)  # dawdles then fails
    registry = RecordingRegistry()

    asyncio.run(
        _poll_once({"fast.consumer": fast, "slow.consumer": slow}, registry=registry)
    )

    # BOTH were polled in the same cycle (concurrency, not serialized skip)...
    assert fast.reads == ["fast.consumer"]
    assert slow.reads == ["slow.consumer"]
    # ...and the fast one's heartbeat landed despite the slow one failing.
    assert registry.heartbeat_calls == [("fast.consumer", 8 * GiB)]
    assert VramLeases.get("fast.consumer").held_bytes == 8 * GiB
    # The failing consumer was neither heartbeated nor marked stale.
    assert registry.mark_stale_calls == []
    assert VramLeases.get("slow.consumer").held_bytes == 1 * GiB


# ---------------------------------------------------------------------------
# AC6 — the poll interval is a named, documented 30s constant
# ---------------------------------------------------------------------------


def test_ac6_poll_interval_is_a_named_30s_constant():
    # Named module constant (not an inline magic number) equal to 30s, matching
    # process_gpu_queue_v2's cadence.
    assert hasattr(poller_mod, "VRAM_POLL_INTERVAL_SECONDS")
    assert VRAM_POLL_INTERVAL_SECONDS == 30
    assert isinstance(VRAM_POLL_INTERVAL_SECONDS, int)


# ---------------------------------------------------------------------------
# AC1 — a cycle-level failure never propagates out of / kills the loop
# ---------------------------------------------------------------------------


def test_ac1_single_source_exception_does_not_propagate_out_of_cycle(vram_db):
    """A raising source is isolated within the cycle (gather(return_exceptions))
    — _poll_once returns normally rather than surfacing the exception."""
    _register("self.llamolotl", held=4 * GiB)
    # Must NOT raise out of asyncio.run — a clean return is the assertion.
    asyncio.run(
        _poll_once({"self.llamolotl": FakeSource(raises=RuntimeError("boom"))}, registry=RecordingRegistry())
    )


def test_ac1_cycle_exception_never_breaks_the_loop(monkeypatch):
    """The ``while True`` loop's outer try/except swallows a whole-cycle failure
    and proceeds to the next sleep — one bad cycle can never kill the poller
    (same posture as process_gpu_queue_v2)."""

    class _StopLoop(Exception):
        pass

    cycles = {"n": 0}

    async def _boom_cycle(state_sources, registry=VramLeases):
        cycles["n"] += 1
        raise RuntimeError("whole-cycle failure")

    async def _stop_sleep(interval):
        # Reaching the sleep at all proves the cycle exception was swallowed;
        # break out of the otherwise-infinite loop after the first iteration.
        raise _StopLoop

    monkeypatch.setattr(poller_mod, "_poll_once", _boom_cycle)
    monkeypatch.setattr(poller_mod.asyncio, "sleep", _stop_sleep)

    # If the loop did NOT catch the cycle's RuntimeError we'd see it here; seeing
    # _StopLoop (raised from the sleep) proves the loop survived the bad cycle.
    with pytest.raises(_StopLoop):
        asyncio.run(process_vram_poll_loop({}))
    assert cycles["n"] == 1
