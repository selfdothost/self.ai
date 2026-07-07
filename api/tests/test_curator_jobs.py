# test_curator_jobs.py
import time

from selfai_ui.models.curator_jobs import CuratorJobs, CuratorJobForm, CuratorJobStatusUpdate


def test_insert_pending_when_no_schedule(db_session):
    job = CuratorJobs.insert_new_job("u1", CuratorJobForm(pipeline_id="p1"))
    assert job is not None
    assert job.status == "pending"
    assert job.pipeline_id == "p1"
    assert job.priority == "normal"


def test_insert_scheduled_when_scheduled_for_set(db_session):
    future = int(time.time()) + 3600
    job = CuratorJobs.insert_new_job("u1", CuratorJobForm(pipeline_id="p1", scheduled_for=future))
    assert job.status == "scheduled"
    assert job.scheduled_for == future


def test_insert_priority_is_preserved(db_session):
    job = CuratorJobs.insert_new_job("u1", CuratorJobForm(pipeline_id="p1", priority="high"))
    assert job.priority == "high"


def test_get_all_jobs_empty(db_session):
    assert CuratorJobs.get_all_jobs() == []


def test_get_all_jobs_returns_all(db_session):
    CuratorJobs.insert_new_job("u1", CuratorJobForm(pipeline_id="p1"))
    CuratorJobs.insert_new_job("u2", CuratorJobForm(pipeline_id="p2"))
    assert len(CuratorJobs.get_all_jobs()) == 2


def test_get_all_jobs_newest_first(db_session):
    from selfai_ui.models.curator_jobs import CuratorJob
    import uuid
    base = int(time.time())
    # Insert directly with controlled timestamps to avoid same-second flakiness
    older = CuratorJob(id=str(uuid.uuid4()), user_id="u1", pipeline_id="p1",
                       status="pending", priority="normal", created_at=base, updated_at=base)
    newer = CuratorJob(id=str(uuid.uuid4()), user_id="u2", pipeline_id="p2",
                       status="pending", priority="normal", created_at=base + 1, updated_at=base + 1)
    db_session.add(older)
    db_session.add(newer)
    db_session.commit()
    all_jobs = CuratorJobs.get_all_jobs()
    # ORDER BY created_at DESC — newer should come first
    assert all_jobs[0].id == newer.id


def test_get_job_by_id_found(db_session):
    job = CuratorJobs.insert_new_job("u1", CuratorJobForm(pipeline_id="p1"))
    result = CuratorJobs.get_job_by_id(job.id)
    assert result is not None
    assert result.id == job.id


def test_get_job_by_id_not_found(db_session):
    assert CuratorJobs.get_job_by_id("nope") is None


def test_update_job_status(db_session):
    job = CuratorJobs.insert_new_job("u1", CuratorJobForm(pipeline_id="p1"))
    result = CuratorJobs.update_job_status(job.id, CuratorJobStatusUpdate(status="queued"))
    assert result.status == "queued"


def test_update_job_status_sets_curator_fields(db_session):
    job = CuratorJobs.insert_new_job("u1", CuratorJobForm(pipeline_id="p1"))
    result = CuratorJobs.update_job_status(
        job.id,
        CuratorJobStatusUpdate(status="running", curator_job_id="remote-abc", curator_url_idx=0),
    )
    assert result.status == "running"
    assert result.curator_job_id == "remote-abc"
    assert result.curator_url_idx == 0


def test_update_job_status_sets_error(db_session):
    job = CuratorJobs.insert_new_job("u1", CuratorJobForm(pipeline_id="p1"))
    result = CuratorJobs.update_job_status(
        job.id,
        CuratorJobStatusUpdate(status="failed", error_message="Something went wrong"),
    )
    assert result.status == "failed"
    assert result.error_message == "Something went wrong"


def test_get_jobs_by_status_filters_correctly(db_session):
    CuratorJobs.insert_new_job("u1", CuratorJobForm(pipeline_id="p1"))
    CuratorJobs.insert_new_job("u2", CuratorJobForm(pipeline_id="p2"))
    pending = CuratorJobs.get_jobs_by_status("pending")
    assert len(pending) == 2
    assert CuratorJobs.get_jobs_by_status("running") == []


def test_get_due_scheduled_jobs(db_session):
    past = int(time.time()) - 60
    future = int(time.time()) + 3600
    job_due = CuratorJobs.insert_new_job("u1", CuratorJobForm(pipeline_id="p1", scheduled_for=past))
    job_future = CuratorJobs.insert_new_job("u2", CuratorJobForm(pipeline_id="p2", scheduled_for=future))
    due = CuratorJobs.get_due_scheduled_jobs()
    due_ids = [j.id for j in due]
    assert job_due.id in due_ids
    assert job_future.id not in due_ids


def test_get_due_scheduled_jobs_excludes_pending(db_session):
    # pending jobs (no scheduled_for) should not appear in due list
    CuratorJobs.insert_new_job("u1", CuratorJobForm(pipeline_id="p1"))
    assert CuratorJobs.get_due_scheduled_jobs() == []


def test_delete_job(db_session):
    job = CuratorJobs.insert_new_job("u1", CuratorJobForm(pipeline_id="p1"))
    assert CuratorJobs.delete_job_by_id(job.id) is True
    assert CuratorJobs.get_job_by_id(job.id) is None


def test_update_job_meta(db_session):
    job = CuratorJobs.insert_new_job("u1", CuratorJobForm(pipeline_id="p1"))
    result = CuratorJobs.update_job_meta(job.id, {"pipeline_config": {"steps": []}})
    assert result.meta == {"pipeline_config": {"steps": []}}


def test_update_job_meta_merges(db_session):
    job = CuratorJobs.insert_new_job("u1", CuratorJobForm(pipeline_id="p1"))
    CuratorJobs.update_job_meta(job.id, {"key1": "a"})
    result = CuratorJobs.update_job_meta(job.id, {"key2": "b"})
    assert result.meta["key1"] == "a"
    assert result.meta["key2"] == "b"
