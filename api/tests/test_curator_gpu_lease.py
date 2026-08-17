"""Curator dispatch takes an exclusive lease, and window end is enforced
(self.ai#88).

Two gaps this pins, both in ``utils/gpu_queue.py``:

  * **Nothing told the broker.** ``lease_admission`` already treated an active
    curator window as a hard inference lockout, but that was WINDOW-driven only —
    the broker itself was never informed, so it kept granting VRAM to other
    consumers for the duration of a curation run. ``acquire_exclusive`` existed,
    was tested, and had ZERO production callers; curator is now the first.
  * **Nothing enforced window END.** ``_fits_in_window`` is consulted for eval
    types only, so a curator job dispatched a minute before its window closed
    kept the whole 4090 indefinitely.

The lease must also stay strictly optional: a deployment with no
``CURATOR_VRAM_CAPACITY_BYTES`` has no registered consumer, and curation there
must behave exactly as it did before this feature rather than silently stop.
"""

import asyncio
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from selfai_ui.models.curator_jobs import CuratorJob, CuratorJobModel, CuratorJobs
from selfai_ui.models.job_windows import JobWindowSlotModel, JobWindowWithSlots
from selfai_ui.utils import gpu_queue
from selfai_ui.utils.vram_broker import ExclusiveResult, HolderAsked

GiB = 1024**3


def _curator_row(db_session, status="queued", curator_job_id=None, priority="normal"):
    row = CuratorJob(
        id=str(uuid.uuid4()),
        user_id="u1",
        pipeline_id="kb-1",
        status=status,
        priority=priority,
        curator_job_id=curator_job_id,
        curator_url_idx=0 if curator_job_id else None,
        meta={"pipeline_config": {"input_path": "/nope", "output_path": "/nope"}},
        created_at=int(time.time()),
        updated_at=int(time.time()),
    )
    db_session.add(row)
    db_session.commit()
    return CuratorJobModel.model_validate(row)


def _window(*job_types):
    now = int(time.time())
    return JobWindowWithSlots(
        id="w1",
        name="w",
        start_at=now - 60,
        end_at=now + 3600,
        preferred_job_type=job_types[0] if job_types else "curator",
        created_at=now,
        updated_at=now,
        slots=[
            JobWindowSlotModel(id=t, window_id="w1", job_type=t, max_concurrent=1)
            for t in job_types
        ],
    )


@pytest.fixture
def app_state():
    state = SimpleNamespace(
        config=SimpleNamespace(
            CURATOR_BASE_URLS=["http://self-curator:8094"],
            CURATOR_CONTROL_BASE_URL="http://self-curator:8094",
        )
    )
    original = gpu_queue._app_state
    gpu_queue._app_state = state
    yield state
    gpu_queue._app_state = original


def _registered(monkeypatch, yes=True):
    monkeypatch.setattr(gpu_queue, "_curator_is_lease_consumer", lambda: yes)


# ── dispatch takes the card ─────────────────────────────────────


def test_dispatch_acquires_the_card_before_shipping_anything(
    db_session, app_state, monkeypatch
):
    job = _curator_row(db_session)
    _registered(monkeypatch)
    acquire = AsyncMock(
        return_value=ExclusiveResult(
            acquired=True, consumer_id="self.curator", reason="card cleared"
        )
    )

    with patch("selfai_ui.utils.vram_broker.VramBroker.acquire_exclusive", acquire):
        with patch("httpx.AsyncClient") as client:
            client.return_value.__aenter__.return_value.post = AsyncMock(
                side_effect=RuntimeError("stop here — the lease is what's under test")
            )
            asyncio.run(gpu_queue._dispatch_curator_job(job))

    acquire.assert_awaited_once_with("self.curator")


def test_a_refused_lease_leaves_the_job_queued_with_the_reason(
    db_session, app_state, monkeypatch
):
    """The job must not start, must not fail, and must explain itself — an
    admin watching a `queued` row that never moves has nothing to act on."""
    job = _curator_row(db_session)
    _registered(monkeypatch)
    refusal = ExclusiveResult(
        acquired=False,
        consumer_id="self.curator",
        reason="could not clear the card",
        reclaimed=[
            HolderAsked(
                consumer_id="self.llamolotl",
                requested_release=10 * GiB,
                outcome="denied",
                reason="serving an active conversation",
            )
        ],
    )
    posted = AsyncMock()

    with patch(
        "selfai_ui.utils.vram_broker.VramBroker.acquire_exclusive",
        AsyncMock(return_value=refusal),
    ):
        with patch("httpx.AsyncClient") as client:
            client.return_value.__aenter__.return_value.post = posted
            asyncio.run(gpu_queue._dispatch_curator_job(job))

    posted.assert_not_awaited()  # nothing was shipped to curator
    after = CuratorJobs.get_job_by_id(job.id)
    assert after.status == "queued"
    assert "self.llamolotl" in after.error_message
    assert "serving an active conversation" in after.error_message


def test_an_unregistered_curator_dispatches_exactly_as_before(
    db_session, app_state, monkeypatch
):
    """A deployment that never wired CURATOR_VRAM_CAPACITY_BYTES has no lease to
    take. The lease must never become a hidden precondition that silently stops
    curation on a deployment that did not opt in."""
    job = _curator_row(db_session)
    _registered(monkeypatch, yes=False)
    acquire = AsyncMock()

    with patch("selfai_ui.utils.vram_broker.VramBroker.acquire_exclusive", acquire):
        with patch("httpx.AsyncClient") as client:
            client.return_value.__aenter__.return_value.post = AsyncMock(
                side_effect=RuntimeError("dispatch proceeded")
            )
            asyncio.run(gpu_queue._dispatch_curator_job(job))

    acquire.assert_not_awaited()


def test_a_failed_dispatch_hands_the_card_back(db_session, app_state, monkeypatch):
    """The card was seized for a run that never started — leaving it held would
    lock every other consumer out over a dispatch error."""
    job = _curator_row(db_session)
    _registered(monkeypatch)
    release = AsyncMock(return_value=True)

    with patch(
        "selfai_ui.utils.vram_broker.VramBroker.acquire_exclusive",
        AsyncMock(
            return_value=ExclusiveResult(
                acquired=True, consumer_id="self.curator", reason="ok"
            )
        ),
    ):
        with patch("selfai_ui.utils.vram_broker.VramBroker.release_exclusive", release):
            with patch("httpx.AsyncClient") as client:
                client.return_value.__aenter__.return_value.post = AsyncMock(
                    side_effect=RuntimeError("curator unreachable")
                )
                asyncio.run(gpu_queue._dispatch_curator_job(job))

    release.assert_awaited_once_with("self.curator")
    assert CuratorJobs.get_job_by_id(job.id).status == "failed"


def test_the_card_is_not_handed_back_while_a_sibling_still_runs(
    db_session, monkeypatch
):
    """With max_concurrent above 1, one job finishing must not release the lease
    out from under another that is still mid-pipeline."""
    _curator_row(db_session, status="running")
    _registered(monkeypatch)
    release = AsyncMock(return_value=True)

    with patch("selfai_ui.utils.vram_broker.VramBroker.release_exclusive", release):
        asyncio.run(gpu_queue._release_curator_card("a sibling settled"))

    release.assert_not_awaited()


def test_the_card_is_handed_back_when_nothing_is_running(db_session, monkeypatch):
    _registered(monkeypatch)
    release = AsyncMock(return_value=True)

    with patch("selfai_ui.utils.vram_broker.VramBroker.release_exclusive", release):
        asyncio.run(gpu_queue._release_curator_card("last job settled"))

    release.assert_awaited_once_with("self.curator")


# ── window end ──────────────────────────────────────────────────


def test_an_open_curator_window_is_left_alone(db_session, monkeypatch):
    job = _curator_row(db_session, status="running")
    _registered(monkeypatch)

    asyncio.run(gpu_queue._enforce_curator_window_end(_window("curator")))

    assert CuratorJobs.get_job_by_id(job.id).status == "running"


def test_an_overrun_is_recorded_and_the_lockout_dropped_by_default(
    db_session, monkeypatch
):
    """Default (CURATOR_ENFORCE_WINDOW_END off): the run continues — NeMo
    Curator has no checkpointing, so killing it would restart from stage 0 — but
    the overrun is recorded and the exclusive lease is dropped so chat is no
    longer hard-locked out. The pipeline's real VRAM is still on the ledger via
    the poller, so this is not the same as pretending it freed anything."""
    job = _curator_row(db_session, status="running")
    _registered(monkeypatch)
    monkeypatch.setattr(gpu_queue, "CURATOR_ENFORCE_WINDOW_END", False)
    release = AsyncMock(return_value=True)

    with patch("selfai_ui.utils.vram_broker.VramBroker.release_exclusive", release):
        asyncio.run(gpu_queue._enforce_curator_window_end(None))

    after = CuratorJobs.get_job_by_id(job.id)
    assert after.status == "running"  # not killed
    assert "Overrunning its GPU window" in after.error_message
    release.assert_awaited_once_with("self.curator")


def test_enforcement_on_stops_and_requeues_the_overrun(db_session, monkeypatch):
    job = _curator_row(db_session, status="running", curator_job_id="remote-1")
    _registered(monkeypatch)
    monkeypatch.setattr(gpu_queue, "CURATOR_ENFORCE_WINDOW_END", True)
    gpu_queue._app_state = SimpleNamespace(
        config=SimpleNamespace(CURATOR_BASE_URLS=["http://self-curator:8094"])
    )
    cancel = AsyncMock()

    try:
        with patch("selfai_ui.utils.vram_broker.VramBroker.release_exclusive", AsyncMock()):
            with patch("httpx.AsyncClient") as client:
                client.return_value.__aenter__.return_value.post = cancel
                asyncio.run(gpu_queue._enforce_curator_window_end(None))
    finally:
        gpu_queue._app_state = None

    # The remote run was cancelled ...
    assert "remote-1" in cancel.await_args[0][0]
    # ... and OUR row went back in the queue for the next window.
    after = CuratorJobs.get_job_by_id(job.id)
    assert after.status == "queued"
    assert "requeued" in after.error_message.lower()


def test_a_training_window_is_not_a_curator_window(db_session, monkeypatch):
    """The slot type is the grant — an open window for something else does not
    entitle a curation run to keep the card."""
    job = _curator_row(db_session, status="running")
    _registered(monkeypatch)
    monkeypatch.setattr(gpu_queue, "CURATOR_ENFORCE_WINDOW_END", False)

    with patch("selfai_ui.utils.vram_broker.VramBroker.release_exclusive", AsyncMock()):
        asyncio.run(gpu_queue._enforce_curator_window_end(_window("training")))

    assert "Overrunning" in CuratorJobs.get_job_by_id(job.id).error_message


def test_nothing_running_is_a_clean_no_op(db_session, monkeypatch):
    _registered(monkeypatch)
    release = AsyncMock()

    with patch("selfai_ui.utils.vram_broker.VramBroker.release_exclusive", release):
        asyncio.run(gpu_queue._enforce_curator_window_end(None))

    release.assert_not_awaited()


# ── requeue semantics ───────────────────────────────────────────


def test_requeue_clears_the_remote_job_id(db_session):
    """self.curator's job records are per-run and a cancelled one cannot be
    resumed. Leaving the old id attached would make the next sync poll a dead
    remote job and immediately mark ours failed."""
    job = _curator_row(db_session, status="running", curator_job_id="remote-1")

    CuratorJobs.requeue_for_next_window(job.id, "stopped by the e-stop")

    after = CuratorJobs.get_job_by_id(job.id)
    assert after.status == "queued"
    assert after.curator_job_id is None
    assert after.curator_url_idx is None
    assert after.error_message == "stopped by the e-stop"


def test_requeue_keeps_the_pipeline_config_so_the_next_window_can_rerun(db_session):
    job = _curator_row(db_session, status="running", curator_job_id="remote-1")

    CuratorJobs.requeue_for_next_window(job.id, "stopped")

    assert CuratorJobs.get_job_by_id(job.id).meta["pipeline_config"] is not None
