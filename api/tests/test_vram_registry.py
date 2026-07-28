"""R1 VRAM lease registry unit tests — covers AC1–AC8.

GPU-less, self-contained: each test drives a temp file-backed SQLite DB with
only the vram_consumer table, patching the vram_leases module's get_db to point
at it. No live GPU, no HTTP, no probing.
"""

import inspect
import time
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import selfai_ui.models.vram_leases as vram_leases
from selfai_ui.models.vram_leases import (
    LEASE_STATE_STALE,
    LEASE_STATE_STEADY,
    STALE_THRESHOLD_SECONDS,
    VramConsumer,
    VramConsumerRegisterForm,
    VramLeases,
)

# 24 GiB card (the yard's single shared 4090), sub-amounts for holders.
CARD = 24 * 1024**3
_STALE_AGE = STALE_THRESHOLD_SECONDS + 100


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
    db_file = tmp_path / "vram_registry_test.db"
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


def _age(vram_db, consumer_id, age_seconds):
    """Backdate a consumer's last_reported_at to simulate elapsed time."""
    db = vram_db["Session"]()
    row = db.query(VramConsumer).filter_by(consumer_id=consumer_id).first()
    row.last_reported_at = int(time.time()) - age_seconds
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# AC1 — register with stable id + total capacity; idempotent
# ---------------------------------------------------------------------------


def test_ac1_register_then_reregister_updates_no_duplicate(vram_db):
    _register("self.llamolotl", held=8 * 1024**3, priority=0)
    # Re-register the same id with new values.
    _register("self.llamolotl", held=10 * 1024**3, priority=1)

    all_consumers = VramLeases.get_all()
    assert len(all_consumers) == 1  # no duplicate row
    got = VramLeases.get("self.llamolotl")
    assert got.held_bytes == 10 * 1024**3  # updated in place
    assert got.priority == 1


# ---------------------------------------------------------------------------
# AC2 — tracks held, last-reported timestamp, lease state (+ all fields)
# ---------------------------------------------------------------------------


def test_ac2_all_tracked_fields_round_trip(vram_db):
    _register("self.llamolotl", held=8 * 1024**3, priority=2)
    got = VramLeases.get("self.llamolotl")
    assert got.consumer_id == "self.llamolotl"
    assert got.total_capacity_bytes == CARD
    assert got.held_bytes == 8 * 1024**3
    assert got.priority == 2
    assert got.lease_state == LEASE_STATE_STEADY
    assert got.last_reported_at is not None
    assert got.created_at is not None
    assert got.updated_at is not None


# ---------------------------------------------------------------------------
# AC3 — total held + free (known total − sum held) across consumers
# ---------------------------------------------------------------------------


def test_ac3_free_capacity_is_total_minus_sum_held(vram_db):
    # Two consumers sharing the one card: both report the same card total.
    _register("self.llamolotl", held=8 * 1024**3)
    _register("self.curator", held=4 * 1024**3)

    assert VramLeases.total_held() == 12 * 1024**3
    # Shared-pool total is the card size (max), not the sum of reported totals.
    assert VramLeases.total_capacity() == CARD
    assert VramLeases.free_capacity() == CARD - 12 * 1024**3

    summary = VramLeases.capacity_summary()
    assert summary.total_held_bytes == 12 * 1024**3
    assert summary.free_bytes == CARD - 12 * 1024**3
    assert len(summary.consumers) == 2


# ---------------------------------------------------------------------------
# AC4 — held only from self-report / confirmed release; never probes
# ---------------------------------------------------------------------------


def test_ac4_no_probe_path_in_module_source():
    """Structural: the registry must never probe/guess VRAM. Assert the module
    reaches for no GPU/HTTP probe surface (the pattern it deliberately
    replaces)."""
    src = inspect.getsource(vram_leases)
    assert "httpx" not in src
    assert "nvidia" not in src.lower()
    assert "/v1/models" not in src


def test_ac4_read_methods_never_mutate_held(vram_db):
    _register("self.llamolotl", held=8 * 1024**3)
    # Exercise every read path repeatedly.
    for _ in range(3):
        VramLeases.total_held()
        VramLeases.total_capacity()
        VramLeases.free_capacity()
        VramLeases.capacity_summary()
        VramLeases.eligible_holders()
        VramLeases.is_stale(VramLeases.get("self.llamolotl"))
    assert VramLeases.get("self.llamolotl").held_bytes == 8 * 1024**3


def test_ac4_heartbeat_writes_self_reported_held(vram_db):
    _register("self.llamolotl", held=8 * 1024**3)
    VramLeases.heartbeat("self.llamolotl", held_bytes=6 * 1024**3)
    assert VramLeases.get("self.llamolotl").held_bytes == 6 * 1024**3


# ---------------------------------------------------------------------------
# AC5 — stale flagged distinguishably, held intact, not 0/dropped
# ---------------------------------------------------------------------------


def test_ac5_stale_consumer_surfaces_stale_with_held_intact(vram_db):
    _register("self.llamolotl", held=8 * 1024**3)
    _age(vram_db, "self.llamolotl", _STALE_AGE)

    got = VramLeases.get("self.llamolotl")
    assert got is not None  # not dropped
    assert got.held_bytes == 8 * 1024**3  # not zeroed
    assert VramLeases.is_stale(got) is True
    assert VramLeases.effective_state(got) == LEASE_STATE_STALE

    # Surfaced distinguishably in the summary too.
    summary = VramLeases.capacity_summary()
    entry = next(c for c in summary.consumers if c.consumer_id == "self.llamolotl")
    assert entry.effective_state == LEASE_STATE_STALE
    assert entry.is_stale is True
    assert entry.held_bytes == 8 * 1024**3


def test_ac5_heartbeat_clears_stale(vram_db):
    _register("self.llamolotl", held=8 * 1024**3)
    _age(vram_db, "self.llamolotl", _STALE_AGE)
    assert VramLeases.is_stale(VramLeases.get("self.llamolotl")) is True

    VramLeases.heartbeat("self.llamolotl", held_bytes=8 * 1024**3)
    got = VramLeases.get("self.llamolotl")
    assert VramLeases.is_stale(got) is False
    assert VramLeases.effective_state(got) == LEASE_STATE_STEADY


# ---------------------------------------------------------------------------
# AC6 — heartbeat staleness excludes from eligible-holder pool (+ order)
# ---------------------------------------------------------------------------


def test_ac6_eligible_holders_excludes_stale(vram_db):
    _register("self.llamolotl", held=8 * 1024**3, priority=0)
    _register("self.curator", held=4 * 1024**3, priority=1)
    _age(vram_db, "self.curator", _STALE_AGE)

    holders = VramLeases.eligible_holders()
    ids = [h.consumer_id for h in holders]
    assert "self.curator" not in ids
    assert "self.llamolotl" in ids


def test_ac6_eligible_holders_ascending_priority(vram_db):
    _register("high", held=2 * 1024**3, priority=5)
    _register("low", held=2 * 1024**3, priority=1)
    _register("mid", held=2 * 1024**3, priority=3)

    holders = VramLeases.eligible_holders()
    assert [h.consumer_id for h in holders] == ["low", "mid", "high"]


def test_ac6_eligible_holders_excludes_zero_held(vram_db):
    _register("holder", held=2 * 1024**3)
    _register("idle", held=0)
    ids = [h.consumer_id for h in VramLeases.eligible_holders()]
    assert ids == ["holder"]


# ---------------------------------------------------------------------------
# AC7 — stale held never auto-freed; only explicit reconcile clears it
# ---------------------------------------------------------------------------


def test_ac7_elapsed_time_never_auto_frees(vram_db):
    _register("self.llamolotl", held=8 * 1024**3)
    _age(vram_db, "self.llamolotl", _STALE_AGE * 10)  # very stale
    # Reads that see staleness must not free anything.
    VramLeases.total_held()
    VramLeases.eligible_holders()
    VramLeases.capacity_summary()
    assert VramLeases.get("self.llamolotl").held_bytes == 8 * 1024**3
    assert VramLeases.total_held() == 8 * 1024**3


def test_ac7_reconcile_is_the_only_thing_that_clears_held(vram_db):
    _register("self.llamolotl", held=8 * 1024**3)
    _age(vram_db, "self.llamolotl", _STALE_AGE)

    result = VramLeases.reconcile("self.llamolotl")
    assert result.held_bytes == 0
    assert result.lease_state == LEASE_STATE_STEADY
    # Row still present (reconciled, not dropped).
    assert VramLeases.get("self.llamolotl") is not None
    assert VramLeases.total_held() == 0


# ---------------------------------------------------------------------------
# AC8 — registry survives a core process restart (persisted)
# ---------------------------------------------------------------------------


def test_ac8_registry_survives_restart(tmp_path):
    db_file = tmp_path / "restart.db"
    url = f"sqlite:///{db_file}"

    original = vram_leases.get_db

    # First "process": create schema, register a consumer, then dispose.
    engine1 = create_engine(url, connect_args={"check_same_thread": False})
    VramConsumer.__table__.create(engine1)
    _, get_db1 = _make_session_get_db(engine1)
    vram_leases.get_db = get_db1
    try:
        _register("self.llamolotl", held=8 * 1024**3, priority=0)
    finally:
        vram_leases.get_db = original
    engine1.dispose()

    # Second "process": fresh engine on the same file — state must persist.
    engine2 = create_engine(url, connect_args={"check_same_thread": False})
    _, get_db2 = _make_session_get_db(engine2)
    vram_leases.get_db = get_db2
    try:
        got = VramLeases.get("self.llamolotl")
        assert got is not None
        assert got.held_bytes == 8 * 1024**3
        assert got.total_capacity_bytes == CARD
    finally:
        vram_leases.get_db = original
        engine2.dispose()
