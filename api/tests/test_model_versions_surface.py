"""Model surface collapse + the producer path — self.ai#131 R5, R7, R9.

The surface half pins Decision 10: a line appears **once**, with its versions
inside it. The producer half pins that three quantizations of one source land
as three versions of one line rather than three lines, and that a registration
survives a self.corpus outage.

Async cases use `asyncio.run(...)` from a sync test — this repo registers no
pytest-asyncio and runs under `--strict-markers`.
"""

import asyncio
import copy
from types import SimpleNamespace

import pytest

import selfai_ui.utils.model_versions as service
from selfai_ui.models.model_versions import ModelLines, ModelVersions
from selfai_ui.utils.model_versions import (
    attach_lines_to_models,
    record_version_for_artifact,
)
from selfai_ui.utils.self_corpus import SelfCorpusError

REPO = "selfai-line-abc"


@pytest.fixture
def app_state():
    return SimpleNamespace(
        config=SimpleNamespace(
            ENABLE_SELF_CORPUS=True,
            SELF_CORPUS_LAKEFS_ENDPOINT="http://terminus.test",
            SELF_CORPUS_LAKEFS_ACCESS_KEY_ID="AKIATEST",
            SELF_CORPUS_LAKEFS_SECRET_ACCESS_KEY="secrettest",
        )
    )


@pytest.fixture
def corpus(monkeypatch):
    """A self.corpus that accepts staging, repo creation and commits."""
    staged = []
    repos = []
    commits = []

    async def _commit(endpoint, key, secret, repo_id, message, metadata=None):
        commits.append({"repo": repo_id, "message": message})
        return f"commit-{len(commits)}"

    async def _create_repository(**kwargs):
        repos.append(kwargs["repo_id"])
        return {"id": kwargs["repo_id"]}

    def _stage(corpus_repo, name, manifest):
        staged.append({"repo": corpus_repo, "name": name, "manifest": manifest})
        return True

    monkeypatch.setattr(service, "commit_branch", _commit)
    monkeypatch.setattr(service, "create_repository", _create_repository)
    monkeypatch.setattr(service, "_stage_provenance_manifest", _stage)
    return SimpleNamespace(staged=staged, repos=repos, commits=commits)


####################
# R7 — ingest lands on a line
####################


def _record(app_state, artifact, line_id=None, line_name="gemma", kind="base"):
    return asyncio.run(
        record_version_for_artifact(
            app_state,
            user_id="u1",
            kind=kind,
            artifact_ref=artifact,
            manifest={"model": artifact},
            line_id=line_id,
            line_name=line_name,
        )
    )


def test_a_new_line_gets_its_own_corpus_repo(db_session, app_state, corpus):
    version = _record(app_state, "gemma-q4.gguf")
    line = ModelLines.get_line_by_id(version.line_id)

    assert line.corpus_repo == f"selfai-line-{line.id}"
    assert corpus.repos == [line.corpus_repo]


def test_three_quantizations_land_as_three_versions_of_one_line(db_session, app_state, corpus):
    """Not three lines — this is the whole point of R7."""
    first = _record(app_state, "gemma-q4.gguf")
    second = _record(app_state, "gemma-q6.gguf", line_id=first.line_id)
    third = _record(app_state, "gemma-q8.gguf", line_id=first.line_id)

    assert {second.line_id, third.line_id} == {first.line_id}
    history = ModelVersions.get_versions_by_line(first.line_id)
    assert [v.sequence for v in history] == [1, 2, 3]
    assert [v.parent_version_id for v in history] == [None, first.id, second.id]
    assert len(ModelLines.get_lines()) == 1


def test_an_adapter_records_the_lines_base(db_session, app_state, corpus):
    base = _record(app_state, "gemma-q4.gguf")
    adapter = _record(app_state, "ada.gguf", line_id=base.line_id, kind="adapter")

    assert adapter.kind == "adapter"
    assert adapter.base_version_id == base.id


def test_a_failed_commit_records_nothing(db_session, app_state, corpus, monkeypatch):
    async def _explode(*args, **kwargs):
        raise SelfCorpusError("self.corpus commit failed (503)")

    monkeypatch.setattr(service, "commit_branch", _explode)

    with pytest.raises(SelfCorpusError):
        _record(app_state, "gemma-q4.gguf")

    line = ModelLines.get_lines()[0]
    assert ModelVersions.get_versions_by_line(line.id) == []


def test_staging_failure_records_nothing(db_session, app_state, corpus, monkeypatch):
    """No manifest means nothing to commit, so there is no version to record."""
    monkeypatch.setattr(service, "_stage_provenance_manifest", lambda *a, **k: False)

    with pytest.raises(service.ModelVersionError, match="provenance manifest"):
        _record(app_state, "gemma-q4.gguf")

    assert corpus.commits == []


def test_provenance_manifest_is_staged_before_the_commit(db_session, app_state, corpus):
    _record(app_state, "gemma-q4.gguf")

    assert corpus.staged[0]["manifest"] == {"model": "gemma-q4.gguf"}
    assert len(corpus.commits) == 1


####################
# R9 — the surface collapses a line to one entry
####################


def _entry(model_id, line_id=None, version_id=None):
    meta = {"description": None}
    if line_id:
        meta = {**meta, "line_id": line_id, "version_id": version_id}
    return {
        "id": model_id,
        "name": model_id,
        "object": "model",
        "owned_by": "llamolotl",
        "info": {"id": model_id, "meta": meta},
    }


def test_a_model_with_no_line_is_returned_untouched(db_session):
    entries = [_entry("plain-model")]
    before = copy.deepcopy(entries)

    after = attach_lines_to_models(entries)

    assert after == before


def test_a_line_with_three_versions_yields_one_entry(db_session, app_state, corpus):
    first = _record(app_state, "gemma-q4.gguf")
    second = _record(app_state, "gemma-q6.gguf", line_id=first.line_id)
    third = _record(app_state, "gemma-q8.gguf", line_id=first.line_id)
    ModelLines.set_current_version(first.line_id, second.id)

    entries = [
        _entry("gemma-q4.gguf", first.line_id, first.id),
        _entry("gemma-q6.gguf", first.line_id, second.id),
        _entry("gemma-q8.gguf", first.line_id, third.id),
        _entry("unrelated-model"),
    ]

    after = attach_lines_to_models(entries)

    assert [e["id"] for e in after] == ["gemma-q6.gguf", "unrelated-model"]
    line_payload = after[0]["line"]
    assert line_payload["version_id"] == second.id
    assert line_payload["current_version_id"] == second.id
    assert [v["sequence"] for v in line_payload["versions"]] == [1, 2, 3]
    assert "line" not in after[1]


def test_the_entry_kept_is_the_lines_current_version(db_session, app_state, corpus):
    first = _record(app_state, "gemma-q4.gguf")
    second = _record(app_state, "gemma-q6.gguf", line_id=first.line_id)
    ModelLines.set_current_version(first.line_id, first.id)

    after = attach_lines_to_models(
        [_entry("gemma-q4.gguf", first.line_id, first.id), _entry("gemma-q6.gguf", first.line_id, second.id)]
    )

    assert [e["id"] for e in after] == ["gemma-q4.gguf"]


def test_the_highest_version_wins_when_no_current_is_set(db_session, app_state, corpus):
    first = _record(app_state, "gemma-q4.gguf")
    second = _record(app_state, "gemma-q6.gguf", line_id=first.line_id)

    after = attach_lines_to_models(
        [_entry("gemma-q4.gguf", first.line_id, first.id), _entry("gemma-q6.gguf", first.line_id, second.id)]
    )

    assert [e["id"] for e in after] == ["gemma-q6.gguf"]


def test_an_entry_naming_a_vanished_line_is_left_alone(db_session):
    """Half-annotating would be worse than not annotating."""
    entries = [_entry("orphan", "no-such-line", "no-such-version")]
    before = copy.deepcopy(entries)

    after = attach_lines_to_models(entries)

    assert after == before


def test_a_single_version_line_renders_as_one_entry_with_its_history(db_session, app_state, corpus):
    """Users who never touch versioning should not notice this shipped."""
    only = _record(app_state, "solo.gguf")
    line = ModelLines.get_line_by_id(only.line_id)

    after = attach_lines_to_models([_entry("solo.gguf", line.id, only.id)])

    assert len(after) == 1
    assert after[0]["line"]["versions"][0]["id"] == only.id


####################
# The two call sites, not just the shared helper
####################


def _model_row(model_id="gemma-q4.gguf", meta=None):
    from selfai_ui.models.models import ModelForm, ModelMeta, ModelParams, Models

    return Models.insert_new_model(
        ModelForm(
            id=model_id,
            name=model_id,
            meta=ModelMeta(**(meta or {"hf_repo": "google/gemma", "quant": "Q4_K_M"})),
            params=ModelParams(),
        ),
        "u1",
    )


def test_registration_links_the_model_row_to_its_version(db_session, app_state, corpus):
    """R7: the Model row keeps a pointer; the provenance lives on the version."""
    from selfai_ui.models.models import Models
    from selfai_ui.routers.llamolotl import HFModelRegisterForm, _attach_registration_to_line

    _model_row()
    request = SimpleNamespace(app=SimpleNamespace(state=app_state))
    user = SimpleNamespace(id="u1")

    asyncio.run(
        _attach_registration_to_line(request, HFModelRegisterForm(name="/models/gemma-q4.gguf"), user)
    )

    model = Models.get_model_by_id("gemma-q4.gguf")
    meta = model.meta.model_dump()
    version = ModelVersions.get_version_by_id(meta["version_id"])
    assert version is not None
    assert meta["line_id"] == version.line_id
    # Carried into provenance, not duplicated as a second source of truth.
    assert version.meta["manifest"]["lineage"]["hf_repo"] == "google/gemma"
    assert meta["hf_repo"] == "google/gemma"


def test_registration_survives_a_corpus_outage(db_session, app_state, corpus, monkeypatch):
    """A self.corpus outage must not make a model unservable."""
    from selfai_ui.models.models import Models
    from selfai_ui.routers import llamolotl as llamolotl_router

    async def _explode(*args, **kwargs):
        raise SelfCorpusError("self.corpus is unreachable")

    monkeypatch.setattr(service, "commit_branch", _explode)

    _model_row()
    request = SimpleNamespace(app=SimpleNamespace(state=app_state))
    user = SimpleNamespace(id="u1")

    # The route wraps this call in its own try/except; the failure surfaces
    # here, and the caller's model row is left servable and unannotated.
    with pytest.raises(SelfCorpusError):
        asyncio.run(
            llamolotl_router._attach_registration_to_line(
                request, llamolotl_router.HFModelRegisterForm(name="gemma-q4.gguf"), user
            )
        )

    model = Models.get_model_by_id("gemma-q4.gguf")
    assert model is not None and model.is_active
    assert model.meta.model_dump().get("version_id") is None


def test_a_completed_training_job_records_an_adapter(db_session, app_state, corpus, monkeypatch):
    """R5's cheap tier: an adapter version, and no base GGUF."""
    import selfai_ui.utils.gpu_queue as gpu_queue
    from selfai_ui.models.training import TrainingJobModel

    base = _record(app_state, "gemma-q4.gguf")
    _model_row(meta={"line_id": base.line_id, "version_id": base.id})
    monkeypatch.setattr(gpu_queue, "_app_state", app_state)

    job = TrainingJobModel(
        id="job-1",
        course_id="course-1",
        user_id="u1",
        model_id="gemma-q4.gguf",
        status="completed",
        priority="normal",
        created_at=1,
        updated_at=1,
    )
    asyncio.run(gpu_queue._record_trained_adapter(job, {"output_path": "adapters/ada.gguf"}))

    history = ModelVersions.get_versions_by_line(base.line_id)
    assert [v.kind for v in history] == ["base", "adapter"]
    assert history[-1].artifact_ref == "adapters/ada.gguf"
    assert history[-1].produced_by == {"job_kind": "training", "job_id": "job-1"}


def test_recording_an_adapter_never_breaks_the_queue_tick(db_session, app_state, corpus, monkeypatch):
    """Every other job's status reconciliation runs behind this call."""
    import selfai_ui.utils.gpu_queue as gpu_queue
    from selfai_ui.models.training import TrainingJobModel

    async def _explode(*args, **kwargs):
        raise SelfCorpusError("self.corpus is unreachable")

    monkeypatch.setattr(service, "commit_branch", _explode)
    monkeypatch.setattr(gpu_queue, "_app_state", app_state)

    job = TrainingJobModel(
        id="job-2",
        course_id="course-1",
        user_id="u1",
        model_id="unknown-model",
        status="completed",
        priority="normal",
        created_at=1,
        updated_at=1,
    )

    asyncio.run(gpu_queue._record_trained_adapter(job, {}))  # must not raise
