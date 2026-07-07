"""
T-500..T-506: Shared GPU-queued job state machine tests.

Covers training + eval jobs. Pipeline (curator) job coverage uses CuratorJobFactory.
Kit: cavekit-ui-job-state-machines.md R1, R2, R3.

Expected outcomes:
- Some transitions the router already enforces correctly → PASS
- Some missing enforcement (e.g., cancel-on-cancelled is 400 not 2xx no-op per R2) → XFAIL
- All xfails point to specific router/model file:line evidence for follow-up fix cycle
"""

import pytest
import time
import uuid
from aioresponses import aioresponses

from tests.factories import (
    TrainingCourseFactory,
    TrainingJobFactory,
    EvalJobFactory,
    CuratorJobFactory,
)


CANONICAL_STATES = {
    "pending",
    "scheduled",
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
}

TERMINAL_STATES = {"completed", "failed", "cancelled"}


# ---------------------------------------------------------------------------
# T-500: State vocabulary invariant
# Kit R1 AC1: canonical state set is exactly the 7-state vocabulary
# ---------------------------------------------------------------------------

@pytest.mark.tier1
def test_training_job_accepts_all_canonical_states(db_session, test_admin):
    """R1-AC1: each canonical state is writable to a TrainingJob record."""
    course = TrainingCourseFactory.create(db_session, user_id=test_admin["id"])
    for state in CANONICAL_STATES:
        job = TrainingJobFactory.create(
            db_session,
            user_id=test_admin["id"],
            course_id=course.id,
            status=state,
        )
        assert job.status == state, (
            f"Training model rejected canonical state {state!r}"
        )


@pytest.mark.tier1
def test_eval_job_accepts_all_canonical_states(db_session, test_admin):
    """R1-AC1: each canonical state is writable to an EvalJob record."""
    for state in CANONICAL_STATES:
        job = EvalJobFactory.create(
            db_session,
            user_id=test_admin["id"],
            status=state,
        )
        assert job.status == state


@pytest.mark.tier1
def test_pipeline_job_accepts_all_canonical_states(db_session, test_admin):
    """R1-AC1: each canonical state is writable to a CuratorJob (pipeline) record."""
    for state in CANONICAL_STATES:
        job = CuratorJobFactory.create(
            db_session,
            user_id=test_admin["id"],
            status=state,
        )
        assert job.status == state


# ---------------------------------------------------------------------------
# T-502: Reject-vs-cancel distinguishability
# Kit R1 AC8: reject writes a rejection marker distinct from user-cancel
# ---------------------------------------------------------------------------

def _make_training_job(db_session, user_id, course_id, status="pending"):
    return TrainingJobFactory.create(
        db_session,
        user_id=user_id,
        course_id=course_id,
        status=status,
    )


@pytest.mark.tier1
def test_reject_training_job_writes_rejection_marker(
    authenticated_admin, db_session, test_admin
):
    """R1-AC8: rejecting a pending training job sets error_message to the rejection marker."""
    course = TrainingCourseFactory.create(db_session, user_id=test_admin["id"])
    job = _make_training_job(db_session, test_admin["id"], course.id, "pending")

    resp = authenticated_admin.post(f"/api/v1/training/jobs/{job.id}/reject")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "cancelled", (
        f"Rejected job should be in cancelled state, got {body['status']!r}"
    )
    # The rejection marker (error_message) distinguishes rejection from cancel
    marker = body.get("error_message") or ""
    assert "reject" in marker.lower(), (
        f"Rejected job should carry a rejection marker distinguishable from a "
        f"user-initiated cancel; error_message was {marker!r}"
    )


@pytest.mark.tier1
def test_cancel_training_job_does_not_set_rejection_marker(
    authenticated_admin, db_session, test_admin
):
    """R1-AC8 (inverse): a user-initiated cancel does NOT carry a rejection marker."""
    course = TrainingCourseFactory.create(db_session, user_id=test_admin["id"])
    job = _make_training_job(db_session, test_admin["id"], course.id, "pending")

    resp = authenticated_admin.post(f"/api/v1/training/jobs/{job.id}/cancel")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "cancelled"
    marker = body.get("error_message") or ""
    # The word "reject" must not appear in the marker for a user-initiated cancel
    assert "reject" not in marker.lower(), (
        f"User-initiated cancel should NOT carry a rejection marker; "
        f"error_message was {marker!r}"
    )


@pytest.mark.tier1
def test_reject_scheduled_job_allowed_per_kit(
    authenticated_admin, db_session, test_admin
):
    """R1-AC8: reject should transition scheduled → cancelled with marker."""
    course = TrainingCourseFactory.create(db_session, user_id=test_admin["id"])
    job = _make_training_job(db_session, test_admin["id"], course.id, "scheduled")

    resp = authenticated_admin.post(f"/api/v1/training/jobs/{job.id}/reject")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


# ---------------------------------------------------------------------------
# T-503: Transition endpoint idempotency (R2)
# Kit R2: honest API — no-op for same-state, 4xx for illegal
# ---------------------------------------------------------------------------


@pytest.mark.tier1
def test_cancel_already_cancelled_is_2xx_noop(
    authenticated_admin, db_session, test_admin
):
    """R2-AC1: cancel-on-cancelled returns 2xx and does not mutate."""
    course = TrainingCourseFactory.create(db_session, user_id=test_admin["id"])
    job = _make_training_job(db_session, test_admin["id"], course.id, "cancelled")

    resp = authenticated_admin.post(f"/api/v1/training/jobs/{job.id}/cancel")
    assert 200 <= resp.status_code < 300, (
        f"Cancel-on-cancelled must be 2xx no-op; got {resp.status_code}: "
        f"{resp.text[:200]}"
    )


@pytest.mark.tier1
def test_approve_queued_job_returns_4xx(
    authenticated_admin, db_session, test_admin
):
    """R2-AC3: approving a queued job is 4xx (not already-approved no-op)."""
    course = TrainingCourseFactory.create(db_session, user_id=test_admin["id"])
    job = _make_training_job(db_session, test_admin["id"], course.id, "queued")

    resp = authenticated_admin.post(f"/api/v1/training/jobs/{job.id}/approve")
    assert 400 <= resp.status_code < 500, (
        f"Approve-on-queued must be 4xx; got {resp.status_code}"
    )


@pytest.mark.tier1
def test_approve_running_job_returns_4xx(
    authenticated_admin, db_session, test_admin
):
    """R2-AC3: approving a running job is 4xx."""
    course = TrainingCourseFactory.create(db_session, user_id=test_admin["id"])
    job = _make_training_job(db_session, test_admin["id"], course.id, "running")

    resp = authenticated_admin.post(f"/api/v1/training/jobs/{job.id}/approve")
    assert 400 <= resp.status_code < 500


@pytest.mark.tier1
def test_approve_completed_job_returns_4xx(
    authenticated_admin, db_session, test_admin
):
    """R2-AC3: approving a terminal-state job is 4xx."""
    course = TrainingCourseFactory.create(db_session, user_id=test_admin["id"])
    job = _make_training_job(db_session, test_admin["id"], course.id, "completed")

    resp = authenticated_admin.post(f"/api/v1/training/jobs/{job.id}/approve")
    assert 400 <= resp.status_code < 500


@pytest.mark.tier1
def test_cancel_completed_job_returns_4xx(
    authenticated_admin, db_session, test_admin
):
    """R2-AC4: cancelling a completed job is 4xx (cannot undo)."""
    course = TrainingCourseFactory.create(db_session, user_id=test_admin["id"])
    job = _make_training_job(db_session, test_admin["id"], course.id, "completed")

    resp = authenticated_admin.post(f"/api/v1/training/jobs/{job.id}/cancel")
    assert 400 <= resp.status_code < 500


@pytest.mark.tier1
def test_cancel_failed_job_returns_4xx(
    authenticated_admin, db_session, test_admin
):
    """R2-AC4: cancelling a failed job is 4xx (cannot undo)."""
    course = TrainingCourseFactory.create(db_session, user_id=test_admin["id"])
    job = _make_training_job(db_session, test_admin["id"], course.id, "failed")

    resp = authenticated_admin.post(f"/api/v1/training/jobs/{job.id}/cancel")
    assert 400 <= resp.status_code < 500


@pytest.mark.tier1
def test_approve_pending_job_illegal_state_check(
    authenticated_admin, db_session, test_admin, test_app
):
    """R2-AC3: approving a pending job passes the illegal-state gate.

    The approve endpoint has two gates:
    1. State gate (R2-AC3): status must be pending or scheduled
    2. Business-logic gate: course must have valid datasets

    This test verifies the state gate passes for a pending job — i.e., any
    400 we get is NOT the illegal-state error. A 400 with dataset-validation
    content is acceptable as it indicates the state gate passed.
    """
    course = TrainingCourseFactory.create(db_session, user_id=test_admin["id"])
    job = _make_training_job(db_session, test_admin["id"], course.id, "pending")

    llamolotl_url = "http://self-llamolotl:8080"
    test_app.state.config.LLAMOLOTL_CONTROL_BASE_URLS = [llamolotl_url]

    with aioresponses() as m:
        m.post(
            f"{llamolotl_url}/api/jobs",
            status=200,
            payload={"id": "llamolotl-job-xyz", "status": "queued"},
        )
        resp = authenticated_admin.post(f"/api/v1/training/jobs/{job.id}/approve")

    # Assert the 400 (if any) is NOT the illegal-state error — the marker
    # text "Only pending or scheduled jobs can be approved" must not appear.
    if resp.status_code == 400:
        body_lower = resp.text.lower()
        assert "only pending or scheduled" not in body_lower, (
            f"Approve-on-pending hit the illegal-state gate; got {resp.text[:200]}"
        )


# ---------------------------------------------------------------------------
# T-506: Cancel propagation to upstream (R3-AC5)
# ---------------------------------------------------------------------------


@pytest.mark.tier1
def test_cancel_running_job_propagates_to_llamolotl(
    authenticated_admin, db_session, test_admin, test_app
):
    """R3-AC5: cancelling a job with a llamolotl_job_id fires an upstream DELETE.

    The router catches upstream exceptions silently (best-effort), but the call
    MUST be attempted — verifying by asserting the mock received the DELETE.
    """
    control_url = "http://self-llamolotl:8080"
    test_app.state.config.LLAMOLOTL_CONTROL_BASE_URLS = [control_url]

    course = TrainingCourseFactory.create(db_session, user_id=test_admin["id"])
    job = TrainingJobFactory.create(
        db_session,
        user_id=test_admin["id"],
        course_id=course.id,
        status="running",
        llamolotl_job_id="llamolotl-job-xyz",
        llamolotl_url_idx=0,
    )

    with aioresponses() as m:
        m.delete(
            f"{control_url}/api/jobs/llamolotl-job-xyz",
            status=200,
            payload={"cancelled": True},
        )
        resp = authenticated_admin.post(f"/api/v1/training/jobs/{job.id}/cancel")

        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"
        # Verify the upstream DELETE was issued
        assert len(m.requests) > 0, (
            "Expected at least one upstream request to llamolotl DELETE endpoint"
        )


@pytest.mark.tier1
def test_cancel_local_transitions_regardless_of_upstream_response(
    authenticated_admin, db_session, test_admin, test_app
):
    """R3-AC5: local transitions to cancelled even if upstream returns 5xx.

    Distributed state converges — local cancel is authoritative from user's
    perspective; upstream error only affects the propagation, not the local
    state.
    """
    control_url = "http://self-llamolotl:8080"
    test_app.state.config.LLAMOLOTL_CONTROL_BASE_URLS = [control_url]

    course = TrainingCourseFactory.create(db_session, user_id=test_admin["id"])
    job = TrainingJobFactory.create(
        db_session,
        user_id=test_admin["id"],
        course_id=course.id,
        status="running",
        llamolotl_job_id="llamolotl-job-err",
        llamolotl_url_idx=0,
    )

    with aioresponses() as m:
        m.delete(
            f"{control_url}/api/jobs/llamolotl-job-err",
            status=503,
            payload={"error": "llamolotl unavailable"},
        )
        resp = authenticated_admin.post(f"/api/v1/training/jobs/{job.id}/cancel")

    # Local state MUST advance to cancelled regardless of upstream failure
    assert resp.status_code == 200, (
        f"Local cancel should succeed despite upstream 503; got {resp.status_code}"
    )
    assert resp.json()["status"] == "cancelled"


# ---------------------------------------------------------------------------
# T-504: Sticky-terminal sync (R3-AC1, AC2)
# Kit: terminal local state is immune to divergent upstream sync
# ---------------------------------------------------------------------------


@pytest.mark.tier1
def test_sync_does_not_resurrect_cancelled_job(
    authenticated_admin, db_session, test_admin, test_app
):
    """R3-AC1/AC2: local cancelled must not be flipped by upstream 'running'."""
    control_url = "http://self-llamolotl:8080"
    test_app.state.config.LLAMOLOTL_CONTROL_BASE_URLS = [control_url]

    course = TrainingCourseFactory.create(db_session, user_id=test_admin["id"])
    job = TrainingJobFactory.create(
        db_session,
        user_id=test_admin["id"],
        course_id=course.id,
        status="cancelled",
        llamolotl_job_id="llamolotl-sticky",
        llamolotl_url_idx=0,
    )

    with aioresponses() as m:
        m.get(
            f"{control_url}/api/jobs/llamolotl-sticky",
            status=200,
            payload={"status": "running"},
        )
        resp = authenticated_admin.post(f"/api/v1/training/jobs/{job.id}/sync")

    # Local must stay cancelled; sync is either a no-op or returns cancelled
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled", (
        f"Local cancelled must not be overwritten by upstream 'running'; "
        f"got {resp.json()['status']!r}"
    )


# ---------------------------------------------------------------------------
# T-505: Forward-only sync + upstream-error non-crash (R3-AC3, AC4)
# ---------------------------------------------------------------------------


@pytest.mark.tier1
def test_sync_advances_non_terminal_job_to_upstream_status(
    authenticated_admin, db_session, test_admin, test_app
):
    """R3-AC3: non-terminal local advances to match upstream forward."""
    control_url = "http://self-llamolotl:8080"
    test_app.state.config.LLAMOLOTL_CONTROL_BASE_URLS = [control_url]

    course = TrainingCourseFactory.create(db_session, user_id=test_admin["id"])
    job = TrainingJobFactory.create(
        db_session,
        user_id=test_admin["id"],
        course_id=course.id,
        status="queued",
        llamolotl_job_id="llamolotl-forward",
        llamolotl_url_idx=0,
    )

    with aioresponses() as m:
        m.get(
            f"{control_url}/api/jobs/llamolotl-forward",
            status=200,
            payload={"status": "running"},
        )
        resp = authenticated_admin.post(f"/api/v1/training/jobs/{job.id}/sync")

    assert resp.status_code == 200
    assert resp.json()["status"] == "running"


@pytest.mark.tier1
def test_sync_upstream_5xx_does_not_crash(
    authenticated_admin, db_session, test_admin, test_app
):
    """R3-AC4: upstream 5xx returns error-but-non-crashing status without mutating local."""
    control_url = "http://self-llamolotl:8080"
    test_app.state.config.LLAMOLOTL_CONTROL_BASE_URLS = [control_url]

    course = TrainingCourseFactory.create(db_session, user_id=test_admin["id"])
    job = TrainingJobFactory.create(
        db_session,
        user_id=test_admin["id"],
        course_id=course.id,
        status="running",
        llamolotl_job_id="llamolotl-err",
        llamolotl_url_idx=0,
    )

    with aioresponses() as m:
        m.get(
            f"{control_url}/api/jobs/llamolotl-err",
            status=503,
            payload={"error": "upstream down"},
        )
        resp = authenticated_admin.post(f"/api/v1/training/jobs/{job.id}/sync")

    # Router raises 502 on non-2xx upstream — that's error-but-non-crashing
    assert resp.status_code >= 400, (
        f"Upstream error should surface as 4xx/5xx; got {resp.status_code}"
    )
    assert resp.status_code != 500, (
        f"Router must handle the error, not crash (500 == unhandled). "
        f"Got {resp.status_code}: {resp.text[:200]}"
    )

    # Re-fetch the job: local state should still be 'running' (unmutated)
    refetch = authenticated_admin.get(f"/api/v1/training/jobs/{job.id}")
    assert refetch.status_code == 200
    assert refetch.json()["status"] == "running", (
        "Local state must not be mutated when upstream errors"
    )


# ---------------------------------------------------------------------------
# T-501: Illegal-transition matrix across every transition endpoint.
# Kit R1-AC8, R1-AC9: every non-allowed (from_state, endpoint) pair returns
# 4xx and does not mutate stored state.
#
# Endpoint legality (training + eval share the same contract):
#   approve    legal: {pending, scheduled}                         -> queued
#   reject     legal: {pending, scheduled}                         -> cancelled+marker
#   cancel     legal: {pending, scheduled, queued, running}        -> cancelled
#              cancelled -> 2xx no-op (R2-AC1; tested in T-503)
#              {completed, failed} -> 4xx (tested in T-503)
#   schedule   legal: {pending, scheduled}                         -> scheduled
#   unschedule legal: {scheduled}                                  -> pending
#
# Pipeline (CuratorJob) has no HTTP transition endpoints — state advances
# through the GPU queue runner, covered by T-513/T-514 not here.
# ---------------------------------------------------------------------------

from selfai_ui.models.training import TrainingJobs
from selfai_ui.models.eval_jobs import EvalJobs


_ILLEGAL_APPROVE = ("queued", "running", "completed", "failed", "cancelled")
_ILLEGAL_REJECT = ("queued", "running", "completed", "failed", "cancelled")
_ILLEGAL_SCHEDULE = ("queued", "running", "completed", "failed", "cancelled")
_ILLEGAL_UNSCHEDULE = ("pending", "queued", "running", "completed", "failed", "cancelled")


def _make_eval_job(db_session, user_id, status="pending"):
    return EvalJobFactory.create(db_session, user_id=user_id, status=status)


def _fetch_status(job_type, job_id):
    if job_type == "training":
        return TrainingJobs.get_job_by_id(id=job_id).status
    return EvalJobs.get_job_by_id(id=job_id).status


def _create_job(job_type, db_session, test_admin, from_state):
    if job_type == "training":
        course = TrainingCourseFactory.create(db_session, user_id=test_admin["id"])
        return _make_training_job(db_session, test_admin["id"], course.id, from_state)
    return _make_eval_job(db_session, test_admin["id"], from_state)


def _endpoint_url(job_type, job_id, verb):
    base = "/api/v1/training/jobs" if job_type == "training" else "/api/v1/evaluations/jobs"
    return f"{base}/{job_id}/{verb}"


def _post_transition(client, job_type, job_id, verb):
    url = _endpoint_url(job_type, job_id, verb)
    if verb == "schedule":
        # Schedule requires a future timestamp body; send one so the
        # state-gate is the only thing that can reject us.
        return client.post(url, json={"scheduled_for": int(time.time()) + 3600})
    return client.post(url)


@pytest.mark.tier1
@pytest.mark.parametrize("job_type", ["training", "eval"])
@pytest.mark.parametrize("from_state", _ILLEGAL_APPROVE)
def test_approve_from_illegal_state_returns_4xx_no_mutation(
    authenticated_admin, db_session, test_admin, job_type, from_state
):
    """R1-AC9 + R2-AC3: approve from any non-{pending,scheduled} state is 4xx, state unchanged."""
    job = _create_job(job_type, db_session, test_admin, from_state)

    resp = _post_transition(authenticated_admin, job_type, job.id, "approve")
    assert 400 <= resp.status_code < 500, (
        f"{job_type} approve from {from_state!r}: expected 4xx, got {resp.status_code}"
    )
    assert _fetch_status(job_type, job.id) == from_state, (
        f"{job_type} approve from {from_state!r} mutated state"
    )


@pytest.mark.tier1
@pytest.mark.parametrize("job_type", ["training", "eval"])
@pytest.mark.parametrize("from_state", _ILLEGAL_REJECT)
def test_reject_from_illegal_state_returns_4xx_no_mutation(
    authenticated_admin, db_session, test_admin, job_type, from_state
):
    """R1-AC9 + R2-AC2: reject from any non-{pending,scheduled} state is 4xx, state unchanged."""
    job = _create_job(job_type, db_session, test_admin, from_state)

    resp = _post_transition(authenticated_admin, job_type, job.id, "reject")
    assert 400 <= resp.status_code < 500, (
        f"{job_type} reject from {from_state!r}: expected 4xx, got {resp.status_code}"
    )
    assert _fetch_status(job_type, job.id) == from_state, (
        f"{job_type} reject from {from_state!r} mutated state"
    )


@pytest.mark.tier1
@pytest.mark.parametrize("job_type", ["training", "eval"])
@pytest.mark.parametrize("from_state", _ILLEGAL_SCHEDULE)
def test_schedule_from_illegal_state_returns_4xx_no_mutation(
    authenticated_admin, db_session, test_admin, job_type, from_state
):
    """R1-AC9: schedule from any non-{pending,scheduled} state is 4xx, state unchanged."""
    job = _create_job(job_type, db_session, test_admin, from_state)

    resp = _post_transition(authenticated_admin, job_type, job.id, "schedule")
    assert 400 <= resp.status_code < 500, (
        f"{job_type} schedule from {from_state!r}: expected 4xx, got {resp.status_code}"
    )
    assert _fetch_status(job_type, job.id) == from_state, (
        f"{job_type} schedule from {from_state!r} mutated state"
    )


@pytest.mark.tier1
@pytest.mark.parametrize("job_type", ["training", "eval"])
@pytest.mark.parametrize("from_state", _ILLEGAL_UNSCHEDULE)
def test_unschedule_from_illegal_state_returns_4xx_no_mutation(
    authenticated_admin, db_session, test_admin, job_type, from_state
):
    """R1-AC9: unschedule from any non-scheduled state is 4xx, state unchanged."""
    job = _create_job(job_type, db_session, test_admin, from_state)

    resp = _post_transition(authenticated_admin, job_type, job.id, "unschedule")
    assert 400 <= resp.status_code < 500, (
        f"{job_type} unschedule from {from_state!r}: expected 4xx, got {resp.status_code}"
    )
    assert _fetch_status(job_type, job.id) == from_state, (
        f"{job_type} unschedule from {from_state!r} mutated state"
    )
