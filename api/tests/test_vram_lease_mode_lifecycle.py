"""An exclusive lease cannot outlive the process that took it (self.ai#78).

`reconcile()`, `record_reaped_release()` and `register()` all cleared held and
lease_state but never `lease_mode`. Staleness only MASKS an exclusive holder —
`active_exclusive_holder()` skips stale rows — so the card unlocked while the
holder was unreachable and then RE-LOCKED the moment its pod came back and
registered. Every other consumer's grant denied, no window actually running, and
nothing in the codebase able to clear it short of a manual DB edit.

The first test is that resurrection, written as the sequence it actually is.
"""

import time
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import selfai_ui.models.vram_leases as vram_leases
from selfai_ui.models.vram_leases import (
    LEASE_MODE_EXCLUSIVE,
    LEASE_MODE_SHARED,
    STALE_THRESHOLD_SECONDS,
    VramConsumer,
    VramConsumerRegisterForm,
    VramLeases,
)

CARD = 24 * 1024**3
GiB = 1024**3


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
    db_file = tmp_path / "vram_lease_mode_test.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    VramConsumer.__table__.create(engine)
    Session, get_db = _make_session_get_db(engine)

    original = vram_leases.get_db
    vram_leases.get_db = get_db
    yield {"engine": engine, "Session": Session}
    vram_leases.get_db = original
    engine.dispose()


def _register(consumer_id, held=0, priority=0):
    return VramLeases.register(
        VramConsumerRegisterForm(
            consumer_id=consumer_id,
            total_capacity_bytes=CARD,
            held_bytes=held,
            priority=priority,
        )
    )


def _age(vram_db, consumer_id, age_seconds):
    db = vram_db["Session"]()
    row = db.query(VramConsumer).filter_by(consumer_id=consumer_id).first()
    row.last_reported_at = int(time.time()) - age_seconds
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# The resurrection
# ---------------------------------------------------------------------------


def test_a_dead_exclusive_holder_does_not_relock_the_card_when_it_returns(vram_db):
    """curator seizes the card, dies, ages out (card unlocks — correct), then its
    pod comes back and registers. It must NOT re-seize a window it is not
    running."""
    _register("curator")
    assert VramLeases.set_exclusive("curator", True) is True
    assert VramLeases.active_exclusive_holder().consumer_id == "curator"

    # The pod dies and the row ages past the staleness bound. Staleness MASKS the
    # exclusive holder, so the card correctly unlocks while it is unreachable.
    _age(vram_db, "curator", STALE_THRESHOLD_SECONDS + 60)
    assert VramLeases.active_exclusive_holder() is None

    # A new pod comes up and registers. That is a NEW PROCESS — it cannot be
    # mid-window, because whatever the old one seized died with it.
    _register("curator")

    assert VramLeases.get("curator").lease_mode == LEASE_MODE_SHARED
    assert VramLeases.active_exclusive_holder() is None, "the card re-locked itself"


def test_register_clears_a_live_exclusive_flag_too(vram_db):
    """Not only the stale case: a re-registration at all means the process
    restarted, so the window is gone."""
    _register("curator")
    VramLeases.set_exclusive("curator", True)

    _register("curator")

    assert VramLeases.get("curator").lease_mode == LEASE_MODE_SHARED


def test_reconcile_clears_the_exclusive_flag(vram_db):
    """An operator asserting the consumer is dead is also asserting it holds no
    window."""
    _register("curator", held=8 * GiB)
    VramLeases.set_exclusive("curator", True)

    VramLeases.reconcile("curator")

    assert VramLeases.get("curator").lease_mode == LEASE_MODE_SHARED
    assert VramLeases.active_exclusive_holder() is None


def test_force_reap_clears_the_exclusive_flag(vram_db):
    """Its pod is confirmed gone; it holds no window either."""
    _register("curator", held=8 * GiB)
    VramLeases.set_exclusive("curator", True)

    VramLeases.record_reaped_release("curator")

    assert VramLeases.get("curator").lease_mode == LEASE_MODE_SHARED
    assert VramLeases.active_exclusive_holder() is None


# ---------------------------------------------------------------------------
# ...without breaking the window that IS running
# ---------------------------------------------------------------------------


def test_a_live_windows_heartbeat_does_not_clear_it(vram_db):
    """The fix must not go the other way. A heartbeat is the SAME process saying
    it is alive, not a new one — an exclusive window that is genuinely running
    has to survive its own heartbeats."""
    _register("curator")
    VramLeases.set_exclusive("curator", True)

    for _ in range(3):
        VramLeases.heartbeat("curator", 12 * GiB)

    assert VramLeases.get("curator").lease_mode == LEASE_MODE_EXCLUSIVE
    assert VramLeases.active_exclusive_holder().consumer_id == "curator"


def test_another_consumers_registration_does_not_disturb_the_window(vram_db):
    """Only the holder's OWN registration clears its flag."""
    _register("curator")
    VramLeases.set_exclusive("curator", True)

    _register("self.speak", held=2 * GiB)

    assert VramLeases.active_exclusive_holder().consumer_id == "curator"


def test_release_exclusive_still_works_normally(vram_db):
    """The ordinary end-of-window path is untouched."""
    _register("curator")
    VramLeases.set_exclusive("curator", True)

    assert VramLeases.set_exclusive("curator", False) is True

    assert VramLeases.get("curator").lease_mode == LEASE_MODE_SHARED
    assert VramLeases.active_exclusive_holder() is None
