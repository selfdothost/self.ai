"""
self.ai#25 (last leg): self.code-eval / self.language-eval service-ticket
forwarding.

evaluations.py uses httpx.AsyncClient for all upstream calls to
self.code-eval and self.language-eval (unlike the llamolotl/curator
routers, which use aiohttp) — tests here use respx directly rather than
the aioresponses_strict() helper those legs use. Every outbound call below
must mint and forward an X-Selfai-Ticket service ticket scoped to the
endpoint it's calling — see api/selfai_ui/routers/evaluations.py and
context/kits/cavekit-service-mesh-ticket-auth.md.

Most of the outbound calls this router makes live in internal
(non-endpoint) functions driven by the background queue processor
(_dispatch_eval_job, _dispatch_language_eval_job, _fetch_code_eval_results,
_sync_running_jobs) rather than directly behind a route the TestClient can
hit — those are exercised here by importing and calling them directly
against a job row inserted straight through EvalJobs, same pattern
test_curator_jobs.py already uses for DB-level fixtures. The two call
sites that *are* directly reachable through a route
(GET /codetests/{result_id}'s sync fallback, POST /jobs/{id}/cancel's
remote-kill call) are exercised through `authenticated_admin` instead.
"""

import time
import uuid

import httpx
import jwt
import pytest
import respx

from selfai_ui.models.eval_jobs import EvalJobs, EvalJobStatusUpdate
from selfai_ui.routers.evaluations import (
    CODE_EVAL_API_URL,
    CODE_EVAL_AUDIENCE,
    LANGUAGE_EVAL_API_URL,
    LANGUAGE_EVAL_AUDIENCE,
    _dispatch_eval_job,
    _dispatch_language_eval_job,
    _fetch_code_eval_results,
    _sync_running_jobs,
)

TEST_SERVICE_AUTH_SECRET = "test-service-auth-secret-not-for-production"


def _decode_ticket(request: httpx.Request, audience: str) -> dict:
    ticket = request.headers.get("X-Selfai-Ticket")
    assert ticket is not None, f"no X-Selfai-Ticket header sent to {request.url}"
    return jwt.decode(
        ticket,
        TEST_SERVICE_AUTH_SECRET,
        algorithms=["HS256"],
        audience=audience,
    )


def _make_job(eval_type: str, benchmark: str = "humaneval"):
    """Insert an EvalJob row directly (mirrors test_curator_jobs.py's
    pattern of hitting the model layer instead of the create-job route,
    since these tests care about the dispatch/sync internals, not job
    creation itself)."""
    from selfai_ui.internal.db import get_db
    from selfai_ui.models.eval_jobs import EvalJob

    with get_db() as db:
        job_id = str(uuid.uuid4())
        now = int(time.time())
        db.add(
            EvalJob(
                id=job_id,
                user_id="u1",
                eval_type=eval_type,
                benchmark=benchmark,
                model_id="test-model",
                status="pending",
                priority="normal",
                meta=None,
                created_at=now,
                updated_at=now,
            )
        )
        db.commit()
    return EvalJobs.get_job_by_id(job_id)


##########################################
# Dispatch: jobs:create
##########################################


@pytest.mark.tier1
def test_dispatch_code_eval_forwards_ticket_with_jobs_create_scope(db_session):
    job = _make_job("code-eval")
    captured = {}

    def _cb(request):
        captured["claims"] = _decode_ticket(request, CODE_EVAL_AUDIENCE)
        return httpx.Response(200, json={"job_id": "remote-1"})

    with respx.mock(assert_all_called=True) as router:
        router.post(f"{CODE_EVAL_API_URL}/api/jobs").mock(side_effect=_cb)
        import asyncio

        asyncio.run(_dispatch_eval_job(job))

    assert captured["claims"]["iss"] == "self.ai"
    assert captured["claims"]["aud"] == CODE_EVAL_AUDIENCE
    assert "jobs:create" in captured["claims"]["scope"].split()


@pytest.mark.tier1
def test_dispatch_language_eval_forwards_ticket_with_jobs_create_scope(db_session):
    job = _make_job("language-eval", benchmark="mmlu")
    captured = {}

    def _cb(request):
        captured["claims"] = _decode_ticket(request, LANGUAGE_EVAL_AUDIENCE)
        return httpx.Response(200, json={"job_id": "remote-2"})

    with respx.mock(assert_all_called=True) as router:
        router.post(f"{LANGUAGE_EVAL_API_URL}/api/jobs").mock(side_effect=_cb)
        import asyncio

        asyncio.run(_dispatch_language_eval_job(job))

    assert captured["claims"]["iss"] == "self.ai"
    assert captured["claims"]["aud"] == LANGUAGE_EVAL_AUDIENCE
    assert "jobs:create" in captured["claims"]["scope"].split()


##########################################
# Status sync: jobs:read
##########################################


@pytest.mark.tier1
def test_sync_running_code_eval_job_forwards_ticket_with_jobs_read_scope(db_session):
    job = _make_job("code-eval")
    EvalJobs.update_job_meta(id=job.id, meta={"code_eval_job_id": "remote-3"})
    EvalJobs.update_job_status(id=job.id, update=EvalJobStatusUpdate(status="running"))
    captured = {}

    def _cb(request):
        captured["claims"] = _decode_ticket(request, CODE_EVAL_AUDIENCE)
        return httpx.Response(200, json={"status": "running"})

    with respx.mock(assert_all_called=True) as router:
        router.get(f"{CODE_EVAL_API_URL}/api/jobs/remote-3").mock(side_effect=_cb)
        import asyncio

        asyncio.run(_sync_running_jobs())

    assert captured["claims"]["aud"] == CODE_EVAL_AUDIENCE
    assert "jobs:read" in captured["claims"]["scope"].split()


@pytest.mark.tier1
def test_sync_running_language_eval_job_forwards_ticket_with_jobs_read_scope(db_session):
    job = _make_job("language-eval", benchmark="mmlu")
    EvalJobs.update_job_meta(id=job.id, meta={"language_eval_job_id": "remote-4"})
    EvalJobs.update_job_status(id=job.id, update=EvalJobStatusUpdate(status="running"))
    captured = {}

    def _cb(request):
        captured["claims"] = _decode_ticket(request, LANGUAGE_EVAL_AUDIENCE)
        return httpx.Response(200, json={"status": "running"})

    with respx.mock(assert_all_called=True) as router:
        router.get(f"{LANGUAGE_EVAL_API_URL}/api/jobs/remote-4").mock(side_effect=_cb)
        import asyncio

        asyncio.run(_sync_running_jobs())

    assert captured["claims"]["aud"] == LANGUAGE_EVAL_AUDIENCE
    assert "jobs:read" in captured["claims"]["scope"].split()


##########################################
# Results fetch: jobs:read
##########################################


@pytest.mark.tier1
def test_fetch_code_eval_results_forwards_ticket_with_jobs_read_scope(db_session, tmp_path, monkeypatch):
    import selfai_ui.routers.evaluations as evaluations_mod

    monkeypatch.setattr(evaluations_mod, "CODE_EVAL_RESULTS_DIR", tmp_path)
    job = _make_job("code-eval")
    captured = {"summary": None, "details": None}

    def _summary_cb(request):
        captured["summary"] = _decode_ticket(request, CODE_EVAL_AUDIENCE)
        return httpx.Response(200, json={"config": {}, "pass@1": {"pass@1": 0.5}})

    def _details_cb(request):
        captured["details"] = _decode_ticket(request, CODE_EVAL_AUDIENCE)
        return httpx.Response(200, json=[{"task": "x"}])

    with respx.mock(assert_all_called=True) as router:
        router.get(f"{CODE_EVAL_API_URL}/api/results/remote-5").mock(side_effect=_summary_cb)
        router.get(f"{CODE_EVAL_API_URL}/api/results/remote-5/details").mock(side_effect=_details_cb)
        import asyncio

        asyncio.run(_fetch_code_eval_results(job, "remote-5"))

    for claims in (captured["summary"], captured["details"]):
        assert claims["aud"] == CODE_EVAL_AUDIENCE
        assert "jobs:read" in claims["scope"].split()


##########################################
# Route-reachable: cancel (jobs:write) + codetests fallback (jobs:read)
##########################################


@pytest.mark.tier1
def test_cancel_running_code_eval_job_forwards_ticket_with_jobs_write_scope(authenticated_admin, db_session):
    job = _make_job("code-eval")
    EvalJobs.update_job_meta(id=job.id, meta={"code_eval_job_id": "remote-6"})
    EvalJobs.update_job_status(id=job.id, update=EvalJobStatusUpdate(status="running"))
    captured = {}

    def _cb(request):
        captured["claims"] = _decode_ticket(request, CODE_EVAL_AUDIENCE)
        return httpx.Response(200, json={"status": "cancelled"})

    with respx.mock(assert_all_called=True) as router:
        router.delete(f"{CODE_EVAL_API_URL}/api/jobs/remote-6").mock(side_effect=_cb)
        resp = authenticated_admin.post(f"/api/v1/evaluations/jobs/{job.id}/cancel")

    assert resp.status_code == 200
    assert captured["claims"]["aud"] == CODE_EVAL_AUDIENCE
    assert "jobs:write" in captured["claims"]["scope"].split()


@pytest.mark.tier1
def test_codetests_details_fallback_forwards_ticket_with_jobs_read_scope(authenticated_admin, tmp_path, monkeypatch):
    """GET /codetests/{result_id} falls back to a live code-eval fetch when
    no local details file exists — that fallback call must be ticketed too."""
    import selfai_ui.routers.evaluations as evaluations_mod

    monkeypatch.setattr(evaluations_mod, "CODE_EVAL_RESULTS_DIR", tmp_path)
    captured = {}

    def _cb(request):
        captured["claims"] = _decode_ticket(request, CODE_EVAL_AUDIENCE)
        return httpx.Response(404)

    with respx.mock(assert_all_called=True) as router:
        router.get(f"{CODE_EVAL_API_URL}/api/results/abc123/details").mock(side_effect=_cb)
        resp = authenticated_admin.get("/api/v1/evaluations/codetests/abc123")

    # Details genuinely absent (mocked 404) -> 404 to the caller, but the
    # forwarded call must still have carried a valid, correctly-scoped ticket.
    assert resp.status_code == 404
    assert captured["claims"]["aud"] == CODE_EVAL_AUDIENCE
    assert "jobs:read" in captured["claims"]["scope"].split()
