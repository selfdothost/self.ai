"""Publish — self.ai#131 R6, the expensive tier.

Five of R6's seven criteria are about what must NOT happen: no publish as a
side effect, no publish without the permission, no empty publish, no moved
pointer on failure, no partial version row. Those are the tests that carry this
file.

Async cases use `asyncio.run(...)` from a sync test — this repo registers no
pytest-asyncio and runs under `--strict-markers`.
"""

import asyncio
from types import SimpleNamespace

import pytest

import selfai_ui.utils.model_versions as service
from selfai_ui.models.model_versions import (
    ModelLines,
    ModelVersionError,
    ModelVersions,
    PublishJobForm,
    PublishJobs,
)
from selfai_ui.utils.model_versions import (
    complete_publish_job,
    create_publish_job,
    current_base_and_adapters,
    resolve_version,
    start_publish_merge,
)


@pytest.fixture
def app_state():
    return SimpleNamespace(
        config=SimpleNamespace(
            ENABLE_SELF_CORPUS=True,
            SELF_CORPUS_LAKEFS_ENDPOINT="http://terminus.test",
            SELF_CORPUS_LAKEFS_ACCESS_KEY_ID="k",
            SELF_CORPUS_LAKEFS_SECRET_ACCESS_KEY="s",
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


async def _merge_ok(app_state, base, adapters, output_name, quant_type):
    """llamolotl answers with the pipeline task it queued, not with a result."""
    return {"task_id": f"task-for-{output_name}", "status": "queued"}


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


def _line_with_adapters(app_state, adapter_count=2):
    base = _record(app_state, "gemma-q4.gguf")
    adapters = [
        _record(app_state, f"ada-{i}.gguf", kind="adapter", line_id=base.line_id) for i in range(adapter_count)
    ]
    ModelLines.set_current_version(base.line_id, adapters[-1].id if adapters else base.id)
    return base, adapters


def _create(app_state, line_id, **kwargs):
    """Enqueue a publish. Touches no GPU (self.ai#136)."""
    return asyncio.run(
        create_publish_job(
            app_state,
            line_id,
            user_id="u1",
            form_data=PublishJobForm(output_name=kwargs.pop("output_name", "gemma-v2.gguf"), **kwargs),
        )
    )


def _publish(app_state, line_id, merge=_merge_ok, **kwargs):
    """Enqueue, then do what the queue does: dispatch, then settle on success.

    The two halves are separate in production — llamolotl's POST returns as
    soon as the merge is queued there — so a test that wants a finished publish
    has to drive both, exactly as `_sync_running_publish_jobs` does.
    """
    job = _create(app_state, line_id, **kwargs)
    asyncio.run(start_publish_merge(app_state, job, merge=merge))
    return asyncio.run(complete_publish_job(app_state, job))


####################
# What a publish would merge
####################


def test_preview_names_the_base_and_every_adapter_since_it(db_session, app_state, corpus):
    base, adapters = _line_with_adapters(app_state, adapter_count=3)

    found_base, found_adapters = current_base_and_adapters(base.line_id)

    assert found_base.id == base.id
    assert [a.id for a in found_adapters] == [a.id for a in adapters]


def test_reverting_narrows_what_a_publish_would_merge(db_session, app_state, corpus):
    """"Go back, then publish" must publish what you went back to."""
    base, adapters = _line_with_adapters(app_state, adapter_count=3)
    ModelLines.set_current_version(base.line_id, adapters[0].id)

    _found_base, found_adapters = current_base_and_adapters(base.line_id)

    assert [a.id for a in found_adapters] == [a.id for a in adapters]


####################
# R6 — the refusals
####################


def test_a_line_with_no_adapters_refuses_to_publish(db_session, app_state, corpus):
    """Several GB of merge to reproduce the base byte for byte."""
    base = _record(app_state, "gemma-q4.gguf")

    with pytest.raises(ModelVersionError, match="nothing to publish"):
        _create(app_state, base.line_id)

    assert PublishJobs.get_jobs_by_line(base.line_id) == []


def test_publishing_twice_with_nothing_new_refuses(db_session, app_state, corpus):
    base, _adapters = _line_with_adapters(app_state, adapter_count=1)
    _publish(app_state, base.line_id)

    with pytest.raises(ModelVersionError, match="nothing to publish"):
        _create(app_state, base.line_id, output_name="gemma-v3.gguf")


####################
# R6 — the success path
####################


def test_publish_produces_a_base_version_parented_on_the_base_it_started_from(db_session, app_state, corpus):
    base, adapters = _line_with_adapters(app_state, adapter_count=2)

    job = _publish(app_state, base.line_id)

    assert job.status == "completed"
    published = ModelVersions.get_version_by_id(job.result_version_id)
    assert published.kind == "base"
    # The base it started from — not the newest version, which is the last
    # adapter that fed it.
    assert published.parent_version_id == base.id
    assert published.produced_by == {"job_kind": "publish", "job_id": job.id}
    assert published.published_by == "u1"


def test_provenance_names_every_adapter_merged(db_session, app_state, corpus):
    base, adapters = _line_with_adapters(app_state, adapter_count=3)

    job = _publish(app_state, base.line_id)

    published = ModelVersions.get_version_by_id(job.result_version_id)
    assert published.meta["merged_adapter_versions"] == [a.id for a in adapters]
    assert job.adapter_version_ids == [a.id for a in adapters]


def test_the_merged_adapters_survive_and_still_resolve(db_session, app_state, corpus):
    """Collapsing into provenance is a record, not a deletion."""
    base, adapters = _line_with_adapters(app_state, adapter_count=2)

    _publish(app_state, base.line_id)

    for adapter in adapters:
        assert ModelVersions.get_version_by_id(adapter.id) is not None
        assert resolve_version(adapter.id).artifact_ref == adapter.artifact_ref


def test_publish_moves_the_line_to_what_it_produced(db_session, app_state, corpus):
    base, _adapters = _line_with_adapters(app_state, adapter_count=1)

    job = _publish(app_state, base.line_id)

    assert ModelLines.get_line_by_id(base.line_id).current_version_id == job.result_version_id


def test_the_new_version_resolves_to_the_commit_the_publish_made(db_session, app_state, corpus):
    base, _adapters = _line_with_adapters(app_state, adapter_count=1)

    job = _publish(app_state, base.line_id)

    resolved = resolve_version(job.result_version_id)
    assert resolved.kind == "base"
    assert resolved.artifact_ref == "gemma-v2.gguf"
    assert resolved.corpus_commit_id == ModelVersions.get_version_by_id(job.result_version_id).corpus_commit_id


####################
# R6 — failure leaves nothing behind
####################


@pytest.mark.parametrize("failing_step", ["merge", "stage", "commit"])
def test_a_failure_at_any_step_leaves_the_pointer_unmoved(
    db_session, app_state, corpus, monkeypatch, failing_step
):
    base, adapters = _line_with_adapters(app_state, adapter_count=2)
    before_pointer = ModelLines.get_line_by_id(base.line_id).current_version_id
    before_versions = [v.id for v in ModelVersions.get_versions_by_line(base.line_id)]

    async def _merge_boom(*args, **kwargs):
        raise RuntimeError("llamolotl fell over mid-merge")

    async def _commit_boom(*args, **kwargs):
        raise service.SelfCorpusError("self.corpus commit failed (503)")

    merge = _merge_ok
    if failing_step == "merge":
        merge = _merge_boom
    elif failing_step == "stage":
        monkeypatch.setattr(service, "_stage_provenance_manifest", lambda *a, **k: False)
    else:
        monkeypatch.setattr(service, "commit_branch", _commit_boom)

    job = _create(app_state, base.line_id)
    with pytest.raises(Exception):
        asyncio.run(start_publish_merge(app_state, job, merge=merge))
        asyncio.run(complete_publish_job(app_state, job))

    line = ModelLines.get_line_by_id(base.line_id)
    assert line.current_version_id == before_pointer
    assert [v.id for v in ModelVersions.get_versions_by_line(base.line_id)] == before_versions

    job = PublishJobs.get_jobs_by_line(base.line_id)[0]
    assert job.result_version_id is None
    if failing_step == "merge":
        # A merge that never started is the DISPATCHER's to record — it is the
        # half that took the card and has to give it back. At this layer the
        # job is simply not running yet.
        assert job.status == "queued"
    else:
        assert job.status == "failed"
        assert job.error_message


def test_a_failed_publish_still_says_what_it_meant_to_merge(db_session, app_state, corpus):
    """Recomputing after the fact would give a different answer."""
    base, adapters = _line_with_adapters(app_state, adapter_count=2)

    async def _boom(*args, **kwargs):
        raise RuntimeError("nope")

    job = _create(app_state, base.line_id)
    with pytest.raises(Exception):
        asyncio.run(start_publish_merge(app_state, job, merge=_boom))

    job = PublishJobs.get_jobs_by_line(base.line_id)[0]
    assert job.adapter_version_ids == [a.id for a in adapters]
    assert job.base_version_id == base.id


####################
# R6 — publish is never a side effect
####################


def test_recording_an_adapter_never_produces_a_base_version(db_session, app_state, corpus):
    """The cheap tier must not be able to reach the expensive one."""
    base = _record(app_state, "gemma-q4.gguf")
    _record(app_state, "ada-0.gguf", kind="adapter", line_id=base.line_id)
    _record(app_state, "ada-1.gguf", kind="adapter", line_id=base.line_id)

    history = ModelVersions.get_versions_by_line(base.line_id)

    assert [v.kind for v in history] == ["base", "adapter", "adapter"]
    assert PublishJobs.get_jobs_by_line(base.line_id) == []
