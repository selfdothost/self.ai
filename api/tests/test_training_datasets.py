"""self.ai#66: dataset resolution for training-job dispatch should say which
dataset failed and why, instead of a single opaque "no valid datasets" message.
"""
import asyncio
import time
import uuid
from unittest.mock import AsyncMock, patch

from selfai_ui.models.knowledge import Knowledge
from selfai_ui.routers.training import _no_valid_datasets_detail, _resolve_course_datasets


def _make_kb(db_session, *, hf_path=None, name="kb", user_id="u1"):
    now = int(time.time())
    kb = Knowledge(
        id=str(uuid.uuid4()),
        user_id=user_id,
        name=name,
        description="",
        data={"hf_path": hf_path} if hf_path else {},
        meta={"dataset": True},
        access_control=None,
        created_at=now,
        updated_at=now,
    )
    db_session.add(kb)
    db_session.commit()
    return kb


def test_resolve_hf_backed_dataset(db_session):
    kb = _make_kb(db_session, hf_path="Abirate/english_quotes", name="english_quotes")
    course_data = {"dataset_ids": [kb.id]}

    with patch(
        "selfai_ui.routers.training._detect_hf_dataset_format",
        new_callable=AsyncMock,
        return_value={"type": "completion", "field": "text"},
    ):
        datasets, failures = asyncio.run(_resolve_course_datasets(course_data, "http://control"))

    assert failures == []
    assert datasets == [{"path": "Abirate/english_quotes", "type": "completion", "field": "text"}]


def test_resolve_missing_dataset_id_reports_which_one(db_session):
    course_data = {"dataset_ids": ["deleted-kb-id"]}
    datasets, failures = asyncio.run(_resolve_course_datasets(course_data, "http://control"))
    assert datasets == []
    assert len(failures) == 1
    assert "deleted-kb-id" in failures[0]
    assert "not found" in failures[0]


def test_resolve_local_dataset_with_no_files_reports_kb_name(db_session):
    kb = _make_kb(db_session, hf_path=None, name="empty-curated-set")
    course_data = {"dataset_ids": [kb.id]}

    with patch(
        "selfai_ui.routers.training._upload_local_dataset",
        new_callable=AsyncMock,
        return_value=[],
    ):
        datasets, failures = asyncio.run(_resolve_course_datasets(course_data, "http://control"))

    assert datasets == []
    assert len(failures) == 1
    assert "empty-curated-set" in failures[0]


def test_resolve_mixed_success_and_failure(db_session):
    good = _make_kb(db_session, hf_path="Abirate/english_quotes", name="good")
    course_data = {"dataset_ids": [good.id, "deleted-kb-id"]}

    with patch(
        "selfai_ui.routers.training._detect_hf_dataset_format",
        new_callable=AsyncMock,
        return_value={"type": "completion", "field": "text"},
    ):
        datasets, failures = asyncio.run(_resolve_course_datasets(course_data, "http://control"))

    assert len(datasets) == 1
    assert len(failures) == 1
    assert "deleted-kb-id" in failures[0]


def test_no_valid_datasets_detail_when_none_attached():
    detail = _no_valid_datasets_detail([], [])
    assert "no datasets attached" in detail.lower()


def test_no_valid_datasets_detail_names_the_failures():
    detail = _no_valid_datasets_detail(["ds-1"], ["ds-1: dataset not found (it may have been deleted)"])
    assert "ds-1: dataset not found" in detail
