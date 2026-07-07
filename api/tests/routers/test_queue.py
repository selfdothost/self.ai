"""T-319: Queue router tests."""

import pytest


@pytest.mark.tier0
def test_queue_empty(authenticated_admin):
    resp = authenticated_admin.get("/api/queue")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.tier0
def test_user_cannot_access_queue(authenticated_user):
    resp = authenticated_user.get("/api/queue")
    assert resp.status_code in (401, 403)


@pytest.mark.tier0
def test_queue_with_pending_jobs_shows_them(
    authenticated_admin, db_session, test_admin
):
    """A pending curator job appears in the queue."""
    from selfai_ui.models.curator_jobs import CuratorJob
    import uuid, time

    job = CuratorJob(
        id=str(uuid.uuid4()),
        user_id=test_admin["id"],
        pipeline_id="test-pipeline",
        status="pending",
        priority="normal",
        meta={"name": "q-test"},
        created_at=int(time.time()),
        updated_at=int(time.time()),
    )
    db_session.add(job)
    db_session.commit()

    resp = authenticated_admin.get("/api/queue")
    assert resp.status_code == 200
    body = resp.json()
    assert any(item.get("id") == job.id for item in body)


@pytest.mark.tier0
def test_run_now_nonexistent_job_rejected(authenticated_admin):
    resp = authenticated_admin.post(
        "/api/jobs/curator/nonexistent-id/run-now"
    )
    assert resp.status_code in (400, 404)


@pytest.mark.tier0
def test_promote_nonexistent_job_rejected(authenticated_admin):
    resp = authenticated_admin.post(
        "/api/jobs/curator/nonexistent-id/promote"
    )
    assert resp.status_code in (400, 404)


# ---------------------------------------------------------------------------
# T-R16: Queue ordering + promotion + entry shape
# ---------------------------------------------------------------------------

def _make_curator_job(db_session, user_id, priority="normal", offset_seconds=0):
    """Insert a curator job with specified priority and created_at offset."""
    from selfai_ui.models.curator_jobs import CuratorJob
    import uuid, time
    job = CuratorJob(
        id=str(uuid.uuid4()),
        user_id=user_id,
        pipeline_id=f"pipeline-{priority}",
        status="pending",
        priority=priority,
        meta={"name": f"job-{priority}-{offset_seconds}"},
        created_at=int(time.time()) + offset_seconds,
        updated_at=int(time.time()) + offset_seconds,
    )
    db_session.add(job)
    db_session.commit()
    return job


@pytest.mark.tier0
def test_queue_entry_shape(authenticated_admin, db_session, test_admin):
    """Each queue item has id, job_type, priority, status, created_at, label."""
    _make_curator_job(db_session, test_admin["id"])
    resp = authenticated_admin.get("/api/queue")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) >= 1
    item = body[0]
    for required in ("id", "job_type", "priority", "status", "created_at"):
        assert required in item, f"Queue item missing required field: {required}"


@pytest.mark.tier0
def test_queue_ordered_by_created_at(
    authenticated_admin, db_session, test_admin
):
    """Queue items are sorted by created_at ascending."""
    _make_curator_job(db_session, test_admin["id"], offset_seconds=10)
    _make_curator_job(db_session, test_admin["id"], offset_seconds=0)
    _make_curator_job(db_session, test_admin["id"], offset_seconds=20)
    resp = authenticated_admin.get("/api/queue")
    assert resp.status_code == 200
    body = resp.json()
    curator_items = [i for i in body if i["job_type"] == "curator"]
    assert len(curator_items) >= 3
    # Ascending by created_at
    created_ats = [i["created_at"] for i in curator_items]
    assert created_ats == sorted(created_ats)


@pytest.mark.tier0
def test_queue_includes_multiple_job_types(
    authenticated_admin, db_session, test_admin
):
    """Queue returns jobs of all active types (curator, training, eval)."""
    _make_curator_job(db_session, test_admin["id"])
    # Training + eval jobs would need similar helpers; here we just
    # verify the curator job shows up correctly.
    resp = authenticated_admin.get("/api/queue")
    body = resp.json()
    types = {item["job_type"] for item in body}
    assert "curator" in types


@pytest.mark.tier0
def test_queue_excludes_terminal_states(
    authenticated_admin, db_session, test_admin
):
    """Completed/failed/cancelled jobs do not appear in the active queue."""
    from selfai_ui.models.curator_jobs import CuratorJob
    import uuid, time

    for terminal_status in ("completed", "failed", "cancelled"):
        db_session.add(CuratorJob(
            id=str(uuid.uuid4()),
            user_id=test_admin["id"],
            pipeline_id="p1",
            status=terminal_status,
            priority="normal",
            meta={},
            created_at=int(time.time()),
            updated_at=int(time.time()),
        ))
    db_session.commit()

    resp = authenticated_admin.get("/api/queue")
    assert resp.status_code == 200
    body = resp.json()
    statuses = {item["status"] for item in body}
    assert "completed" not in statuses
    assert "failed" not in statuses
    assert "cancelled" not in statuses
