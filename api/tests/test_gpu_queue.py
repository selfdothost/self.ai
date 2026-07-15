# test_gpu_queue.py
import asyncio
import time
import uuid
from unittest.mock import AsyncMock, patch

from selfai_ui.models.benchmark_config import BenchmarkConfig
from selfai_ui.models.curator_jobs import CuratorJob
from selfai_ui.models.eval_jobs import EvalJob
from selfai_ui.models.job_windows import JobWindowSlotModel, JobWindowWithSlots
from selfai_ui.models.training import TrainingJob
from selfai_ui.utils.gpu_queue import (
    _dispatch_run_now_jobs,
    _dispatch_window_jobs,
    _fits_in_window,
    _running_count,
)


def test_fits_unknown_benchmark(db_session):
    # No config row → unknown benchmark → always allow
    assert (
        _fits_in_window(
            "unknown-bench",
            "language-eval",
            remaining_minutes=60,
            min_remaining_minutes=0,
        )
        is True
    )


def test_fits_when_duration_fits(db_session):
    # 30-min benchmark, 60 remaining, 5 min buffer → 30 <= (60 - 5) → fits
    db_session.add(
        BenchmarkConfig(
            id=str(uuid.uuid4()),
            benchmark="hellaswag",
            eval_type="language-eval",
            max_duration_minutes=30,
            created_at=int(time.time()),
            updated_at=int(time.time()),
        )
    )
    db_session.commit()
    assert _fits_in_window("hellaswag", "language-eval", remaining_minutes=60, min_remaining_minutes=5) is True


def test_does_not_fit_when_window_too_short(db_session):
    # 60-min benchmark, 30 remaining → 60 <= 30 is False → doesn't fit
    db_session.add(
        BenchmarkConfig(
            id=str(uuid.uuid4()),
            benchmark="humaneval",
            eval_type="code-eval",
            max_duration_minutes=60,
            created_at=int(time.time()),
            updated_at=int(time.time()),
        )
    )
    db_session.commit()
    assert _fits_in_window("humaneval", "code-eval", remaining_minutes=30, min_remaining_minutes=0) is False


# ---- helpers for dispatch tests ----


def _make_training_job(db_session, *, status="queued", priority="normal", created_at=None):
    now = created_at or int(time.time())
    job = TrainingJob(
        id=str(uuid.uuid4()),
        course_id="c1",
        user_id="u1",
        model_id="m1",
        status=status,
        priority=priority,
        created_at=now,
        updated_at=now,
    )
    db_session.add(job)
    db_session.commit()
    return job


def _make_eval_job(
    db_session, *, eval_type="language-eval", status="queued", priority="normal", benchmark="hellaswag", created_at=None
):
    now = created_at or int(time.time())
    job = EvalJob(
        id=str(uuid.uuid4()),
        user_id="u1",
        eval_type=eval_type,
        benchmark=benchmark,
        model_id="m1",
        status=status,
        priority=priority,
        created_at=now,
        updated_at=now,
    )
    db_session.add(job)
    db_session.commit()
    return job


def _make_curator_job(db_session, *, status="queued", priority="normal", created_at=None):
    now = created_at or int(time.time())
    job = CuratorJob(
        id=str(uuid.uuid4()),
        user_id="u1",
        pipeline_id="p1",
        status=status,
        priority=priority,
        created_at=now,
        updated_at=now,
    )
    db_session.add(job)
    db_session.commit()
    return job


def _make_window(*, preferred="training", duration_secs=7200, slots=None):
    now = int(time.time())
    return JobWindowWithSlots(
        id=str(uuid.uuid4()),
        name="test",
        start_at=now - 60,
        end_at=now + duration_secs,
        preferred_job_type=preferred,
        enabled=True,
        created_at=now,
        updated_at=now,
        slots=slots or [],
        status="active",
    )


def _slot(job_type, max_concurrent=1, min_remaining_minutes=0):
    return JobWindowSlotModel(
        id=str(uuid.uuid4()),
        window_id="w1",
        job_type=job_type,
        max_concurrent=max_concurrent,
        min_remaining_minutes=min_remaining_minutes,
    )


# ---- _running_count ----


def test_running_count_empty(db_session):
    assert _running_count("training") == 0
    assert _running_count("language-eval") == 0
    assert _running_count("code-eval") == 0
    assert _running_count("curator") == 0


def test_running_count_training(db_session):
    _make_training_job(db_session, status="running")
    _make_training_job(db_session, status="queued")
    assert _running_count("training") == 1


def test_running_count_eval_types_separate(db_session):
    _make_eval_job(db_session, eval_type="language-eval", status="running")
    _make_eval_job(db_session, eval_type="code-eval", status="running")
    assert _running_count("language-eval") == 1
    assert _running_count("code-eval") == 1


def test_running_count_curator(db_session):
    _make_curator_job(db_session, status="running")
    _make_curator_job(db_session, status="queued")
    assert _running_count("curator") == 1


# ---- _dispatch_run_now_jobs ----


def test_dispatch_run_now_skips_normal_priority(db_session):
    _make_training_job(db_session, priority="normal", status="queued")
    with patch("selfai_ui.utils.gpu_queue._dispatch_training_job", new_callable=AsyncMock) as mock_fn:
        asyncio.run(_dispatch_run_now_jobs())
        mock_fn.assert_not_called()


def test_dispatch_run_now_dispatches_training(db_session):
    job = _make_training_job(db_session, priority="run_now", status="queued")
    with patch("selfai_ui.utils.gpu_queue._dispatch_training_job", new_callable=AsyncMock) as mock_fn:
        asyncio.run(_dispatch_run_now_jobs())
        mock_fn.assert_called_once()
        assert mock_fn.call_args[0][0].id == job.id


def test_dispatch_run_now_dispatches_eval(db_session):
    job = _make_eval_job(db_session, priority="run_now", status="queued", eval_type="language-eval")
    with patch("selfai_ui.utils.gpu_queue._dispatch_eval_job_by_type", new_callable=AsyncMock) as mock_fn:
        asyncio.run(_dispatch_run_now_jobs())
        mock_fn.assert_called_once()
        assert mock_fn.call_args[0][0].id == job.id


# ---- _dispatch_window_jobs ----


def test_dispatch_window_no_slots_does_nothing(db_session):
    _make_training_job(db_session, priority="high", status="queued")
    window = _make_window(slots=[])
    dispatched = []

    async def capture(jtype, job):
        dispatched.append(job.id)

    with patch("selfai_ui.utils.gpu_queue._dispatch_job", side_effect=capture):
        asyncio.run(_dispatch_window_jobs(window))
    assert dispatched == []


def test_dispatch_window_pass1_high_before_normal(db_session):
    base = int(time.time())
    _make_training_job(db_session, priority="normal", status="queued", created_at=base)
    _make_training_job(db_session, priority="high", status="queued", created_at=base + 1)

    window = _make_window(preferred="training", slots=[_slot("training", max_concurrent=2)])

    order = []

    async def capture(jtype, job):
        order.append(job.priority)

    with patch("selfai_ui.utils.gpu_queue._dispatch_job", side_effect=capture):
        asyncio.run(_dispatch_window_jobs(window))

    assert len(order) == 2
    assert order[0] == "high"  # Pass 1
    assert order[1] == "normal"  # Pass 3


def test_dispatch_window_pass3_training_fallback(db_session):
    job = _make_training_job(db_session, priority="normal", status="queued")
    window = _make_window(preferred="training", slots=[_slot("training")])

    dispatched = []

    async def capture(jtype, job):
        dispatched.append((jtype, job.id))

    with patch("selfai_ui.utils.gpu_queue._dispatch_job", side_effect=capture):
        asyncio.run(_dispatch_window_jobs(window))

    assert dispatched == [("training", job.id)]


def test_dispatch_window_respects_max_concurrent(db_session):
    # 1 already running, max_concurrent=1 → queued job not dispatched
    _make_training_job(db_session, status="running")
    _make_training_job(db_session, priority="normal", status="queued")
    window = _make_window(preferred="training", slots=[_slot("training", max_concurrent=1)])

    dispatched = []

    async def capture(jtype, job):
        dispatched.append(job.id)

    with patch("selfai_ui.utils.gpu_queue._dispatch_job", side_effect=capture):
        asyncio.run(_dispatch_window_jobs(window))

    assert dispatched == []


def test_dispatch_window_pass2_preferred_type_first(db_session):
    base = int(time.time())
    _make_curator_job(db_session, priority="normal", status="queued", created_at=base)
    _make_eval_job(
        db_session,
        eval_type="language-eval",
        priority="normal",
        status="queued",
        created_at=base,
    )

    # Preferred type is "curator" → curator dispatched before language-eval in Pass 2
    window = _make_window(
        preferred="curator",
        slots=[_slot("curator"), _slot("language-eval")],
    )

    order = []

    async def capture(jtype, job):
        order.append(jtype)

    with patch("selfai_ui.utils.gpu_queue._dispatch_job", side_effect=capture):
        asyncio.run(_dispatch_window_jobs(window))

    assert order[0] == "curator"
    assert "language-eval" in order
