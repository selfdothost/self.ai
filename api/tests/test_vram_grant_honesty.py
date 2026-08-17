"""The grant path never fabricates a success, and never runs unbounded
(self.ai#77 + self.ai#80).

Two failures that both end with the broker misleading its caller:

  * #77 — every success return did ``held_bytes=... if granted else
    amount_bytes``, so a ``record_grant`` that returned ``None`` (a swallowed DB
    exception, or the row vanishing between the registration check and the
    write) still produced a ``LeaseGranted`` for a hold that was never recorded.
    The consumer then allocates VRAM the registry does not know about.
  * #80 — each release was bounded but the total was not, so one decision could
    hold the broker-wide grant lock for (holders x per-release timeout) while
    every other grant and both exclusive operations queued behind it.

Same GPU-less, temp-SQLite posture as the sibling suites; async cases use
``asyncio.run(...)`` because this repo registers no pytest-asyncio under
``--strict-markers``.
"""

import asyncio
import time
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import selfai_ui.models.vram_leases as vram_leases
from selfai_ui.models.vram_leases import (
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
    _Budget,
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
    db_file = tmp_path / "vram_grant_honesty_test.db"
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


class _Denies:
    async def request_release(self, consumer_id, amount_bytes, timeout_seconds):
        return ReleaseResponse(outcome=ReleaseOutcome.DENIED)


class _SlowHolder:
    """A holder that never answers within its bound, recording the bound it was
    given so a test can watch the budget shrink."""

    def __init__(self):
        self.bounds = []

    async def request_release(self, consumer_id, amount_bytes, timeout_seconds):
        self.bounds.append(timeout_seconds)
        await asyncio.sleep(timeout_seconds + 1)  # never returns in time
        return ReleaseResponse(outcome=ReleaseOutcome.DENIED)


# ---------------------------------------------------------------------------
# #77 — a write that did not land must not be reported as a grant
# ---------------------------------------------------------------------------


def test_failed_grant_write_denies_instead_of_fabricating_a_grant(vram_db, monkeypatch):
    """record_grant swallows DB exceptions and returns None. That must produce a
    DENIAL, never a LeaseGranted for a hold the registry never recorded."""
    _register("trainer")
    monkeypatch.setattr(
        "selfai_ui.models.vram_leases.VramLeases.record_grant",
        lambda *a, **kw: None,
    )
    broker = VramBrokerImpl(transport=_Denies())

    result = asyncio.run(broker.request_lease("trainer", 4 * GiB, priority=0))

    assert isinstance(result, LeaseDenied), "fabricated a grant that was never written"
    assert result.requested_bytes == 4 * GiB


def test_failed_grant_write_leaves_no_hold_behind(vram_db, monkeypatch):
    """The denial is safe precisely because nothing was written — there is no
    phantom hold to unwind."""
    _register("trainer")
    monkeypatch.setattr(
        "selfai_ui.models.vram_leases.VramLeases.record_grant",
        lambda *a, **kw: None,
    )
    broker = VramBrokerImpl(transport=_Denies())

    asyncio.run(broker.request_lease("trainer", 4 * GiB, priority=0))

    row = VramLeases.get("trainer")
    assert row.held_bytes == 0
    assert not row.reserved_bytes
    assert VramLeases.free_capacity() == CARD


def test_successful_write_still_grants_normally(vram_db):
    """The happy path is unchanged — the commit point only intercepts failure."""
    _register("trainer")
    broker = VramBrokerImpl(transport=_Denies())

    result = asyncio.run(broker.request_lease("trainer", 4 * GiB, priority=0))

    assert isinstance(result, LeaseGranted)
    assert result.granted_bytes == 4 * GiB
    assert result.held_bytes == 4 * GiB
    assert VramLeases.get("trainer").reserved_bytes == 4 * GiB


# ---------------------------------------------------------------------------
# #80 — the decision is bounded overall, not just per holder
# ---------------------------------------------------------------------------


def test_budget_shrinks_each_holders_bound_to_what_is_left():
    budget = _Budget(10.0)
    assert budget.bound(30.0) <= 10.0
    assert budget.bound(2.0) == 2.0
    assert not budget.spent()


def test_spent_budget_reports_no_remaining():
    budget = _Budget(0.0)
    assert budget.spent()
    assert budget.remaining() == 0.0
    assert budget.bound(30.0) == 0.0


def test_grant_stops_asking_holders_once_the_budget_is_spent(vram_db, monkeypatch):
    """Three slow holders x a 30s per-release bound used to mean 90s under the
    grant lock. With an overall budget the loop stops early and denies."""
    monkeypatch.setattr(
        "selfai_ui.utils.vram_broker.GRANT_TOTAL_TIMEOUT_SECONDS", 0.3
    )
    _register("trainer", priority=100)
    for name in ("h1", "h2", "h3"):
        _register(name, held=8 * GiB, priority=1)

    transport = _SlowHolder()
    broker = VramBrokerImpl(transport=transport)

    started = time.monotonic()
    result = asyncio.run(
        broker.request_lease(
            "trainer", 20 * GiB, priority=100, release_timeout_seconds=1.0
        )
    )
    elapsed = time.monotonic() - started

    assert isinstance(result, LeaseDenied)
    # Unbounded this is 3 holders x 1s = ~3s of held grant lock. Bounded it is
    # ~0.3s. Generous headroom so a loaded runner cannot make this flaky.
    assert elapsed < 1.5, f"decision took {elapsed:.2f}s"
    # And it stopped rather than walking every holder.
    assert len(transport.bounds) < 3


def test_each_release_bound_never_exceeds_the_remaining_budget(vram_db, monkeypatch):
    """A per-release timeout longer than the remaining budget is clamped, so one
    slow holder cannot overrun the overall bound on its own."""
    monkeypatch.setattr(
        "selfai_ui.utils.vram_broker.GRANT_TOTAL_TIMEOUT_SECONDS", 0.3
    )
    _register("trainer", priority=100)
    _register("h1", held=20 * GiB, priority=1)

    transport = _SlowHolder()
    broker = VramBrokerImpl(transport=transport)

    asyncio.run(
        broker.request_lease(
            "trainer", 22 * GiB, priority=100, release_timeout_seconds=30.0
        )
    )

    assert transport.bounds, "no release was attempted"
    # Asked for 30s, but the overall budget was 0.3s.
    assert transport.bounds[0] <= 0.3


def test_a_bounded_grant_still_banks_what_it_reclaimed(vram_db, monkeypatch):
    """Running out of budget is not a rollback: confirmed releases that already
    landed stay landed, and the denial reports them."""
    _register("trainer", priority=100)
    _register("h1", held=10 * GiB, priority=1)

    class _ConfirmsPartialRelease:
        def __init__(self):
            self.calls = 0

        async def request_release(self, consumer_id, amount_bytes, timeout_seconds):
            self.calls += 1
            return ReleaseResponse(
                outcome=ReleaseOutcome.CONFIRMED, new_held_bytes=6 * GiB
            )

    broker = VramBrokerImpl(transport=_ConfirmsPartialRelease())
    # Ask for more than even the full reclaim can cover, so it ends in denial.
    result = asyncio.run(broker.request_lease("trainer", 23 * GiB, priority=100))

    assert isinstance(result, LeaseDenied)
    # The confirmed partial release is real and recorded — not rolled back.
    assert VramLeases.get("h1").held_bytes == 6 * GiB
    assert [h.consumer_id for h in result.holders_asked] == ["h1"]
