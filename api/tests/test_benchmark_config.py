# test_benchmark_config.py
import time
import uuid

from selfai_ui.models.benchmark_config import (
    BenchmarkConfig,
    BenchmarkConfigs,
    BenchmarkConfigUpdate,
)


def _make_config(db_session, *, benchmark="hellaswag", eval_type="language-eval", max_duration_minutes=30):
    now = int(time.time())
    row = BenchmarkConfig(
        id=str(uuid.uuid4()),
        benchmark=benchmark,
        eval_type=eval_type,
        max_duration_minutes=max_duration_minutes,
        created_at=now,
        updated_at=now,
    )
    db_session.add(row)
    db_session.commit()
    return row


def test_get_all_empty(db_session):
    assert BenchmarkConfigs.get_all() == []


def test_get_all_returns_all(db_session):
    _make_config(db_session, benchmark="hellaswag", eval_type="language-eval")
    _make_config(db_session, benchmark="humaneval", eval_type="code-eval")
    result = BenchmarkConfigs.get_all()
    assert len(result) == 2


def test_get_by_id_found(db_session):
    row = _make_config(db_session)
    result = BenchmarkConfigs.get_by_id(row.id)
    assert result is not None
    assert result.id == row.id
    assert result.benchmark == "hellaswag"


def test_get_by_id_not_found(db_session):
    assert BenchmarkConfigs.get_by_id("nope") is None


def test_get_by_benchmark_found(db_session):
    _make_config(
        db_session,
        benchmark="hellaswag",
        eval_type="language-eval",
        max_duration_minutes=45,
    )
    result = BenchmarkConfigs.get_by_benchmark("hellaswag", "language-eval")
    assert result is not None
    assert result.max_duration_minutes == 45


def test_get_by_benchmark_wrong_eval_type(db_session):
    # Same benchmark but different eval_type → no match
    _make_config(db_session, benchmark="hellaswag", eval_type="language-eval")
    assert BenchmarkConfigs.get_by_benchmark("hellaswag", "code-eval") is None


def test_get_by_benchmark_not_found(db_session):
    assert BenchmarkConfigs.get_by_benchmark("unknown", "language-eval") is None


def test_update_max_duration(db_session):
    row = _make_config(db_session, max_duration_minutes=30)
    result = BenchmarkConfigs.update(row.id, BenchmarkConfigUpdate(max_duration_minutes=90))
    assert result is not None
    assert result.max_duration_minutes == 90


def test_update_adds_notes(db_session):
    row = _make_config(db_session)
    result = BenchmarkConfigs.update(row.id, BenchmarkConfigUpdate(max_duration_minutes=30, notes="fast"))
    assert result.notes == "fast"


def test_update_nonexistent_returns_none(db_session):
    result = BenchmarkConfigs.update("nope", BenchmarkConfigUpdate(max_duration_minutes=60))
    assert result is None
