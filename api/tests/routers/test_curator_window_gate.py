"""Every path to the GPU obeys the admin's curator window (self.ai#88).

The queue path already did: ``POST /curator/queue`` creates a row the daemon
dispatches inside a window. Three ways around it did not, and each is pinned
here:

  1. ``priority`` on /curator/queue was caller-supplied and unvalidated, and
     ``_dispatch_run_now_jobs`` dispatches ``run_now`` "immediately, bypassing
     windows" — so any verified user could self-grant a window bypass by asking
     for one;
  2. ``POST /curator/api/jobs/{id}/approve`` is a raw pass-through that STARTS a
     curation run on the shared 4090. It was reachable by any verified user with
     no window check at all — the widest of the three;
  3. ``POST /curator/api/jobs`` (create) and the schedule proxies were likewise
     open to any verified user.
"""

import time

import pytest

from selfai_ui.models.job_windows import (
    JobWindowForm,
    JobWindows,
    JobWindowSlotForm,
)
from tests.mocks.external_services import aioresponses_strict

CURATOR_URL = "http://self-curator:8094"


def _open_window(job_type):
    now = int(time.time())
    return JobWindows.insert_new_window(
        JobWindowForm(
            name=f"{job_type}-window",
            start_at=now - 60,
            end_at=now + 3600,
            preferred_job_type=job_type,
            slots=[JobWindowSlotForm(job_type=job_type, max_concurrent=1)],
        )
    )


@pytest.fixture
def open_curator_window(db_session):
    return _open_window("curator")


@pytest.fixture
def open_training_window(db_session):
    return _open_window("training")


def _queue_body(priority=None):
    body = {
        "pipeline_id": "kb-1",
        "pipeline_config": {
            "name": "clean",
            "input_path": "/workspace/curator/data/in.jsonl",
            "output_path": "/workspace/curator/data/out",
            "stages": [],
        },
    }
    if priority is not None:
        body["priority"] = priority
    return body


# ── 1. priority self-grant ──────────────────────────────────────


@pytest.mark.tier1
def test_a_plain_user_cannot_self_grant_run_now(authenticated_user):
    """`run_now` bypasses the GPU window outright. Choosing to override the
    admin's schedule is an operator decision, not something a caller asserts
    about itself in a request body."""
    resp = authenticated_user.post("/curator/queue", json=_queue_body("run_now"))

    assert resp.status_code == 403
    assert "run_now" in resp.json()["detail"]


@pytest.mark.tier1
def test_an_admin_can_still_run_now(authenticated_admin):
    """The escalation tier still exists — it is just an escalation now."""
    resp = authenticated_admin.post("/curator/queue", json=_queue_body("run_now"))

    assert resp.status_code == 200
    assert resp.json()["priority"] == "run_now"


@pytest.mark.tier1
@pytest.mark.parametrize("priority", ["normal", "high"])
def test_window_respecting_priorities_are_open_to_any_user(
    authenticated_user, priority
):
    resp = authenticated_user.post("/curator/queue", json=_queue_body(priority))

    assert resp.status_code == 200
    assert resp.json()["priority"] == priority


@pytest.mark.tier1
def test_an_unknown_priority_is_refused_not_silently_downgraded(authenticated_user):
    """Coercing an unrecognised value to `normal` would silently change what the
    caller asked for; refusing says so."""
    resp = authenticated_user.post("/curator/queue", json=_queue_body("urgent!!"))

    assert resp.status_code == 400
    assert "urgent!!" in resp.json()["detail"]


@pytest.mark.tier1
def test_default_priority_is_normal(authenticated_user):
    resp = authenticated_user.post("/curator/queue", json=_queue_body())

    assert resp.status_code == 200
    assert resp.json()["priority"] == "normal"


# ── 2. approve: the direct GPU start ────────────────────────────


@pytest.mark.tier1
def test_approve_is_refused_with_no_open_curator_window(authenticated_admin):
    """No window open -> no curation run starts, even for an admin. This is the
    one proxy that actually puts load on the card."""
    resp = authenticated_admin.post("/curator/api/jobs/abc123/approve")

    assert resp.status_code == 409
    assert "window" in resp.json()["detail"].lower()


@pytest.mark.tier1
def test_approve_is_forbidden_for_a_plain_user(authenticated_user):
    resp = authenticated_user.post("/curator/api/jobs/abc123/approve")

    assert resp.status_code == 401


@pytest.mark.tier1
def test_approve_forwards_when_a_curator_window_is_open(
    authenticated_admin, open_curator_window
):
    with aioresponses_strict() as m:
        m.post(
            f"{CURATOR_URL}/api/jobs/abc123/approve",
            status=200,
            payload={"job_id": "abc123", "status": "running"},
        )
        resp = authenticated_admin.post("/curator/api/jobs/abc123/approve")

    assert resp.status_code == 200
    assert resp.json()["status"] == "running"


@pytest.mark.tier1
def test_approve_fails_closed_when_the_window_table_cannot_be_read(
    authenticated_admin, monkeypatch
):
    """An unknown schedule must not become an open door to the GPU."""

    def _boom():
        raise RuntimeError("yard-pg unreachable")

    monkeypatch.setattr(
        "selfai_ui.models.job_windows.JobWindows.get_active_window", _boom
    )
    resp = authenticated_admin.post("/curator/api/jobs/abc123/approve")

    assert resp.status_code == 503


@pytest.mark.tier1
def test_a_window_without_a_curator_slot_does_not_open_the_door(
    authenticated_admin, open_training_window
):
    """A training window is not a curator window — the slot type is the grant."""
    resp = authenticated_admin.post("/curator/api/jobs/abc123/approve")

    assert resp.status_code == 409


# ── 3. the remaining write proxies ──────────────────────────────


@pytest.mark.tier1
@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/curator/api/jobs"),
        ("post", "/curator/api/jobs/abc123/schedule"),
        ("post", "/curator/api/jobs/abc123/unschedule"),
    ],
)
def test_direct_job_write_proxies_are_admin_only(authenticated_user, method, path):
    resp = getattr(authenticated_user, method)(path, json={"scheduled_for": 1})

    assert resp.status_code == 401


@pytest.mark.tier1
def test_cancel_stays_open_to_any_user(authenticated_user):
    """Cancelling only ever FREES the card, so it is not gated — an admin-only
    stop button would be a worse failure mode than an over-permissive one."""
    with aioresponses_strict() as m:
        m.post(
            f"{CURATOR_URL}/api/jobs/abc123/cancel",
            status=200,
            payload={"status": "cancelled", "job_id": "abc123"},
        )
        resp = authenticated_user.post("/curator/api/jobs/abc123/cancel")

    assert resp.status_code == 200
