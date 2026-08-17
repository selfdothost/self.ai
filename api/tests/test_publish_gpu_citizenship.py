"""A publish queues and leases the GPU — self.ai#136.

The behaviour under test is mostly refusal and release, because that is what
being a good VRAM citizen consists of: do not take the card without asking, and
do not keep it after you are done. A leaked exclusive lease locks the 4090 for
everyone until a human notices.

Async cases use `asyncio.run(...)` from a sync test — this repo registers no
pytest-asyncio and runs under `--strict-markers`.
"""

import asyncio
from types import SimpleNamespace

import pytest

import selfai_ui.utils.gpu_queue as gpu_queue
import selfai_ui.utils.model_versions as service
from selfai_ui.models.model_versions import ModelLines, PublishJobForm, PublishJobs
from selfai_ui.utils.lease_admission import GPU_EXCLUSIVE_WINDOW_JOB_TYPES


@pytest.fixture
def app_state():
    return SimpleNamespace(
        config=SimpleNamespace(
            ENABLE_SELF_CORPUS=True,
            SELF_CORPUS_LAKEFS_ENDPOINT="http://terminus.test",
            SELF_CORPUS_LAKEFS_ACCESS_KEY_ID="k",
            SELF_CORPUS_LAKEFS_SECRET_ACCESS_KEY="s",
            LLAMOLOTL_CONTROL_BASE_URLS=["http://llamolotl.test:8093"],
            LLAMOLOTL_BASE_URLS=["http://llamolotl.test:8080"],
            LLAMOLOTL_API_CONFIGS={},
        )
    )


@pytest.fixture
def corpus(monkeypatch):
    commits = []

    async def _commit(endpoint, key, secret, repo_id, message, metadata=None):
        commits.append(message)
        return f"commit-{len(commits)}"

    async def _create_repository(**kwargs):
        return {"id": kwargs["repo_id"]}

    monkeypatch.setattr(service, "commit_branch", _commit)
    monkeypatch.setattr(service, "create_repository", _create_repository)
    monkeypatch.setattr(service, "_stage_provenance_manifest", lambda *a, **k: True)
    return commits


@pytest.fixture
def queue(monkeypatch, app_state):
    """The queue module wired to a fake broker and a fake llamolotl."""
    monkeypatch.setattr(gpu_queue, "_app_state", app_state)

    state = SimpleNamespace(
        registered=True,
        acquire_result=(True, None),
        acquires=0,
        releases=0,
        merges=[],
        merge_raises=False,
    )

    async def _acquire(job):
        state.acquires += 1
        return state.acquire_result

    async def _release(context):
        state.releases += 1

    async def _merge(app_state_, base, adapters, output_name, quant_type):
        if state.merge_raises:
            raise RuntimeError("llamolotl refused the merge")
        state.merges.append(output_name)
        return {"task_id": f"task-{len(state.merges)}", "status": "queued"}

    monkeypatch.setattr(gpu_queue, "_acquire_publish_card", _acquire)
    monkeypatch.setattr(gpu_queue, "_release_publish_card", _release)
    monkeypatch.setattr(
        service,
        "_publish_merge_for_tests",
        _merge,
        raising=False,
    )

    async def _start(app_state_, job, merge=None):
        return await service.start_publish_merge(app_state_, job, merge=_merge)

    monkeypatch.setattr(gpu_queue, "start_publish_merge", _start)
    return state


def _record(app_state, artifact, kind="base", line_id=None):
    return asyncio.run(
        service.record_version_for_artifact(
            app_state,
            user_id="u1",
            kind=kind,
            artifact_ref=artifact,
            manifest={"artifact": artifact},
            line_id=line_id,
            line_name="gemma",
        )
    )


def _queued_publish(app_state, adapter_count=1, **form):
    base = _record(app_state, "gemma-q4.gguf")
    for i in range(adapter_count):
        _record(app_state, f"ada-{i}.gguf", kind="adapter", line_id=base.line_id)
    ModelLines.set_current_version(base.line_id, base.id)
    job = asyncio.run(
        service.create_publish_job(
            app_state,
            base.line_id,
            user_id="u1",
            form_data=PublishJobForm(output_name="gemma-v2.gguf", **form),
        )
    )
    return base, job


####################
# R1 — creation enqueues, it does not dispatch
####################


def test_creating_a_publish_issues_no_merge(db_session, app_state, corpus, queue):
    _base, job = _queued_publish(app_state)

    assert job.status == "queued"
    assert queue.merges == [], "the request path must not touch the GPU"
    assert queue.acquires == 0


def test_a_scheduled_publish_waits_for_its_time(db_session, app_state, corpus, queue):
    import time

    _base, job = _queued_publish(app_state, scheduled_for=int(time.time()) + 3600)

    assert job.status == "scheduled"
    assert PublishJobs.get_due_scheduled_jobs() == []


def test_a_due_scheduled_publish_becomes_a_candidate(db_session, app_state, corpus, queue):
    import time

    _base, job = _queued_publish(app_state, scheduled_for=int(time.time()) - 60)

    assert [j.id for j in PublishJobs.get_due_scheduled_jobs()] == [job.id]


####################
# R2 — the card is taken before the merge, and given back after
####################


def test_dispatch_acquires_before_it_merges(db_session, app_state, corpus, queue):
    _base, job = _queued_publish(app_state)

    asyncio.run(gpu_queue._dispatch_publish_job(job))

    assert queue.acquires == 1
    assert queue.merges == ["gemma-v2.gguf"]
    assert PublishJobs.get_job_by_id(job.id).status == "running"
    assert PublishJobs.get_job_by_id(job.id).llamolotl_job_id == "task-1"


def test_a_refused_lease_dispatches_nothing(db_session, app_state, corpus, queue):
    queue.acquire_result = (False, "Waiting for the GPU. self.speak: refused — mid-utterance")
    _base, job = _queued_publish(app_state)

    asyncio.run(gpu_queue._dispatch_publish_job(job))

    assert queue.merges == [], "no merge may be issued without the card"
    assert PublishJobs.get_job_by_id(job.id).status == "queued"
    assert queue.releases == 0, "nothing was acquired, so nothing may be released"


def test_a_merge_that_never_starts_gives_the_card_back(db_session, app_state, corpus, queue):
    """A leaked exclusive lease locks the 4090 until a human notices."""
    queue.merge_raises = True
    _base, job = _queued_publish(app_state)

    asyncio.run(gpu_queue._dispatch_publish_job(job))

    assert queue.acquires == 1
    assert queue.releases == 1
    settled = PublishJobs.get_job_by_id(job.id)
    assert settled.status == "failed"
    assert "refused the merge" in settled.error_message


####################
# R3 — a refusal an operator can act on
####################


def test_the_refusal_reason_is_recorded_on_the_job(db_session, app_state, corpus, queue):
    reason = "Waiting for the GPU. self.sketch: refused — rendering"
    queue.acquire_result = (False, reason)
    _base, job = _queued_publish(app_state)

    asyncio.run(gpu_queue._dispatch_publish_job(job))

    assert PublishJobs.get_job_by_id(job.id).meta["waiting_reason"] == reason


def test_an_unchanged_reason_is_not_rewritten_every_tick(db_session, app_state, corpus, queue):
    """A publish waiting through many ticks must not bury its own record."""
    queue.acquire_result = (False, "Waiting for the GPU. self.sketch: refused")
    _base, job = _queued_publish(app_state)

    asyncio.run(gpu_queue._dispatch_publish_job(job))
    first = PublishJobs.get_job_by_id(job.id).updated_at

    asyncio.run(gpu_queue._dispatch_publish_job(job))
    assert PublishJobs.get_job_by_id(job.id).updated_at == first


def test_the_reason_clears_once_the_publish_starts(db_session, app_state, corpus, queue):
    queue.acquire_result = (False, "Waiting for the GPU.")
    _base, job = _queued_publish(app_state)
    asyncio.run(gpu_queue._dispatch_publish_job(job))

    queue.acquire_result = (True, None)
    asyncio.run(gpu_queue._dispatch_publish_job(PublishJobs.get_job_by_id(job.id)))

    assert PublishJobs.get_job_by_id(job.id).meta.get("waiting_reason") is None


####################
# R2/R4 — registration is opt-in, and the decision is exclusive
####################


def test_an_unregistered_deployment_publishes_without_a_lease(db_session, monkeypatch):
    """The lease must never become a hidden precondition that stops publishing."""
    monkeypatch.setattr(gpu_queue.VramLeases, "get", lambda *_a, **_k: None)
    assert gpu_queue._publish_is_lease_consumer() is False


def test_a_registry_read_failure_is_treated_as_unconfigured(db_session, monkeypatch):
    """A yard-pg hiccup must degrade to today's behaviour, not wedge the queue."""

    def _explode(*_a, **_k):
        raise RuntimeError("yard-pg is unhappy")

    monkeypatch.setattr(gpu_queue.VramLeases, "get", _explode)
    assert gpu_queue._publish_is_lease_consumer() is False


def test_publish_locks_inference_out_like_curation_does():
    assert "publish" in GPU_EXCLUSIVE_WINDOW_JOB_TYPES
    assert {"training", "curator"} <= GPU_EXCLUSIVE_WINDOW_JOB_TYPES


def test_the_publish_consumer_is_not_llamolotl():
    """Acquiring as llamolotl would mark the serving process the exclusive
    holder while the serving process is what has to give up its VRAM."""
    assert gpu_queue.PUBLISH_AUDIENCE != gpu_queue.LLAMOLOTL_AUDIENCE


####################
# R1 — the queue knows the job type
####################


def test_running_count_counts_publishes(db_session, app_state, corpus, queue):
    _base, job = _queued_publish(app_state)
    assert gpu_queue._running_count("publish") == 0

    asyncio.run(gpu_queue._dispatch_publish_job(job))
    assert gpu_queue._running_count("publish") == 1


def test_dispatch_job_routes_the_publish_type(db_session, app_state, corpus, queue):
    _base, job = _queued_publish(app_state)

    asyncio.run(gpu_queue._dispatch_job("publish", job))

    assert queue.merges == ["gemma-v2.gguf"]


####################
# The operator surfaces: a publish is a job like any other
####################


def test_the_queue_lists_publishes_alongside_other_jobs(db_session, app_state, corpus, queue):
    from selfai_ui.routers.queue import JOB_TYPES, _publish_to_item

    _base, job = _queued_publish(app_state)

    assert "publish" in JOB_TYPES
    item = _publish_to_item(PublishJobs.get_job_by_id(job.id))
    assert item.job_type == "publish"
    assert item.status == "queued"
    assert item.label.startswith("Publish ")


def test_a_waiting_publish_shows_its_reason_in_the_queue(db_session, app_state, corpus, queue):
    """A row that never moves with no reason is the thing this prevents."""
    from selfai_ui.routers.queue import _publish_to_item

    queue.acquire_result = (False, "Waiting for the GPU. self.sketch: refused — rendering")
    _base, job = _queued_publish(app_state)
    asyncio.run(gpu_queue._dispatch_publish_job(job))

    item = _publish_to_item(PublishJobs.get_job_by_id(job.id))
    assert item.status == "queued"
    assert "self.sketch" in item.status_detail


def test_run_now_still_asks_the_broker(db_session, app_state, corpus, queue):
    """run_now bypasses the WINDOW, not the LEASE.

    Otherwise an admin escalation would be a way to OOM llamolotl on demand.
    """
    queue.acquire_result = (False, "Waiting for the GPU.")
    _base, job = _queued_publish(app_state, priority="run_now")

    asyncio.run(gpu_queue._dispatch_run_now_jobs())

    assert queue.acquires == 1
    assert queue.merges == []
    assert PublishJobs.get_job_by_id(job.id).status == "queued"


def test_run_now_dispatches_a_publish_when_the_card_is_free(db_session, app_state, corpus, queue):
    _base, job = _queued_publish(app_state, priority="run_now")

    asyncio.run(gpu_queue._dispatch_run_now_jobs())

    assert queue.merges == ["gemma-v2.gguf"]
    assert PublishJobs.get_job_by_id(job.id).status == "running"


def test_the_queue_router_can_promote_a_publish(db_session, app_state, corpus, queue):
    from selfai_ui.routers.queue import _get_job_and_table, _update_priority

    _base, job = _queued_publish(app_state)

    found, table = _get_job_and_table("publish", job.id)
    assert found is not None and table is PublishJobs

    _update_priority("publish", job.id, priority="high")
    assert PublishJobs.get_job_by_id(job.id).priority == "high"
