"""Line/version service — self.ai#131, cavekit-model-versioning.md R2/R8.

The ordering guarantee is the substance: validate, then commit, then write the
row. Every failure case below asserts on what *did not* happen — no commit
issued, no row written, no fallback returned — because those are the failures
that leave a line looking healthy while pointing at nothing.

Async cases use `asyncio.run(...)` from a sync test — this repo registers no
pytest-asyncio and runs under `--strict-markers` (see
test_vram_device_occupancy.py).
"""

import asyncio
from types import SimpleNamespace

import pytest

import selfai_ui.utils.model_versions as service
from selfai_ui.models.model_versions import (
    ModelLineForm,
    ModelLines,
    ModelVersion,
    ModelVersionError,
    ModelVersionForm,
    ModelVersions,
)
from selfai_ui.utils.model_versions import (
    ProvenanceBroken,
    VersionUnresolvable,
    append_version,
    latest_version,
    resolve_version,
    revert_line,
    walk_provenance,
)
from selfai_ui.utils.self_corpus import SelfCorpusError

REPO = "selfai-line-abc"


@pytest.fixture
def app_state():
    return SimpleNamespace(
        config=SimpleNamespace(
            SELF_CORPUS_LAKEFS_ENDPOINT="http://terminus.test",
            SELF_CORPUS_LAKEFS_ACCESS_KEY_ID="AKIATEST",
            SELF_CORPUS_LAKEFS_SECRET_ACCESS_KEY="secrettest",
        )
    )


@pytest.fixture
def commits(monkeypatch):
    """Record every commit the service issues, and hand back a fresh id."""
    issued = []

    async def _commit(endpoint, key, secret, repo_id, message, metadata=None):
        issued.append({"repo": repo_id, "message": message, "metadata": metadata})
        return f"commit-{len(issued)}"

    monkeypatch.setattr(service, "commit_branch", _commit)
    return issued


@pytest.fixture
def failing_commits(monkeypatch):
    """self.corpus is reachable enough to try, and refuses."""
    attempts = []

    async def _commit(endpoint, key, secret, repo_id, message, metadata=None):
        attempts.append(repo_id)
        raise SelfCorpusError("self.corpus commit failed (503): upstream is down")

    monkeypatch.setattr(service, "commit_branch", _commit)
    return attempts


def _line(corpus_repo=REPO, name="gemma-line"):
    return ModelLines.insert_new_line("u1", ModelLineForm(name=name, corpus_repo=corpus_repo))


def _append(app_state, line_id, kind="base", parent=None, base=None, artifact="gemma-q4.gguf", **kwargs):
    return asyncio.run(
        append_version(
            app_state,
            line_id,
            kind=kind,
            commit_message=f"{kind} for {line_id}",
            parent_version_id=parent,
            base_version_id=base,
            artifact_ref=artifact,
            **kwargs,
        )
    )


####################
# R2 — commit first, row second
####################


def test_append_records_the_commit_the_commit_returned(db_session, app_state, commits):
    line = _line()
    version = _append(app_state, line.id, produced_by={"job_kind": "pull", "job_id": "j1"})

    assert version.corpus_commit_id == "commit-1"
    assert commits[0]["repo"] == REPO
    assert version.produced_by == {"job_kind": "pull", "job_id": "j1"}


def test_a_failed_commit_writes_no_version(db_session, app_state, failing_commits):
    """The version did not happen — not "happened, pointing nowhere"."""
    line = _line()

    with pytest.raises(SelfCorpusError):
        _append(app_state, line.id)

    assert failing_commits == [REPO], "the commit should have been attempted"
    assert ModelVersions.get_versions_by_line(line.id) == []


def test_an_invalid_append_never_reaches_self_corpus(db_session, app_state, commits):
    """Validation runs before the commit, so a bad append costs nothing.

    A commit made for a version that then fails validation is a stray commit
    nobody will ever find.
    """
    line_a, line_b = _line(name="a"), _line(corpus_repo="selfai-line-b", name="b")
    foreign = _append(app_state, line_a.id)
    commits.clear()

    with pytest.raises(ModelVersionError, match="different line"):
        _append(app_state, line_b.id, parent=foreign.id)

    assert commits == [], "no commit should have been issued for an invalid append"
    assert ModelVersions.get_versions_by_line(line_b.id) == []


def test_append_refuses_a_line_with_no_corpus_repo(db_session, app_state, commits):
    line = _line(corpus_repo=None)

    with pytest.raises(ModelVersionError, match="no self.corpus repo"):
        _append(app_state, line.id)

    assert commits == []


def test_commit_metadata_is_passed_through(db_session, app_state, commits):
    line = _line()
    _append(app_state, line.id, commit_metadata={"job_id": "j1"})
    assert commits[0]["metadata"] == {"job_id": "j1"}


def test_latest_version_tracks_the_appends(db_session, app_state, commits):
    line = _line()
    assert latest_version(line.id) is None
    first = _append(app_state, line.id)
    second = _append(app_state, line.id, kind="adapter", parent=first.id, base=first.id)
    assert latest_version(line.id).id == second.id


####################
# R2 — the provenance walk
####################


def _four_version_line(app_state):
    line = _line()
    first = _append(app_state, line.id)
    second = _append(app_state, line.id, kind="adapter", parent=first.id, base=first.id)
    third = _append(app_state, line.id, kind="adapter", parent=second.id, base=first.id)
    fourth = _append(app_state, line.id, kind="base", parent=third.id)
    return line, [first, second, third, fourth]


def test_walk_reaches_the_first_version(db_session, app_state, commits):
    _line_row, versions = _four_version_line(app_state)
    chain = walk_provenance(versions[-1].id)

    assert [v.id for v in chain] == [v.id for v in reversed(versions)]
    assert chain[-1].parent_version_id is None


def test_walk_names_the_broken_link(db_session, app_state, commits):
    """Not a truncated chain presented as a complete one."""
    _line_row, versions = _four_version_line(app_state)
    orphaned = versions[2]

    db_session.query(ModelVersion).filter_by(id=orphaned.id).update({"parent_version_id": "ghost"})
    db_session.commit()

    with pytest.raises(ProvenanceBroken) as excinfo:
        walk_provenance(versions[-1].id)

    message = str(excinfo.value)
    assert orphaned.id in message and "ghost" in message


def test_walk_refuses_a_cycle(db_session, app_state, commits):
    """Unreachable through the accessors; forced here to pin the guard."""
    _line_row, versions = _four_version_line(app_state)
    first, second = versions[0], versions[1]

    db_session.query(ModelVersion).filter_by(id=first.id).update({"parent_version_id": second.id})
    db_session.commit()

    with pytest.raises(ProvenanceBroken, match="cycles"):
        walk_provenance(versions[-1].id)


def test_walk_refuses_a_self_parent(db_session, app_state, commits):
    line = _line()
    version = _append(app_state, line.id)

    db_session.query(ModelVersion).filter_by(id=version.id).update({"parent_version_id": version.id})
    db_session.commit()

    with pytest.raises(ProvenanceBroken, match="cycles"):
        walk_provenance(version.id)


def test_walk_of_a_missing_version_is_reported(db_session):
    with pytest.raises(ProvenanceBroken, match="not found"):
        walk_provenance("ghost")


####################
# R8 — resolution is a lookup
####################


def test_base_resolves_to_its_own_artifact(db_session, app_state, commits):
    line = _line()
    version = _append(app_state, line.id, artifact="gemma-q4.gguf")

    resolved = resolve_version(version.id)

    assert resolved.kind == "base"
    assert resolved.artifact_ref == "gemma-q4.gguf"
    assert resolved.base_artifact_ref is None
    assert resolved.corpus_repo == REPO
    assert resolved.corpus_commit_id == version.corpus_commit_id


def test_adapter_resolves_to_its_base_plus_itself(db_session, app_state, commits):
    """A caller reading only artifact_ref here would serve a bare LoRA."""
    line = _line()
    base = _append(app_state, line.id, artifact="gemma-q4.gguf")
    adapter = _append(app_state, line.id, kind="adapter", parent=base.id, base=base.id, artifact="ada.gguf")

    resolved = resolve_version(adapter.id)

    assert resolved.artifact_ref == "ada.gguf"
    assert resolved.base_version_id == base.id
    assert resolved.base_artifact_ref == "gemma-q4.gguf"


def test_adapter_without_an_explicit_base_uses_its_nearest_base_ancestor(db_session, app_state, commits):
    line = _line()
    base = _append(app_state, line.id, artifact="gemma-q4.gguf")
    first = _append(app_state, line.id, kind="adapter", parent=base.id, base=base.id, artifact="a1.gguf")
    second = _append(app_state, line.id, kind="adapter", parent=first.id, artifact="a2.gguf")

    resolved = resolve_version(second.id)

    assert resolved.base_version_id == base.id
    assert resolved.base_artifact_ref == "gemma-q4.gguf"


def test_missing_artifact_is_named_and_not_substituted(db_session, app_state, commits):
    """The failure that matters: never answer with a different version."""
    line = _line()
    base = _append(app_state, line.id, artifact="gemma-q4.gguf")
    stale = _append(app_state, line.id, kind="adapter", parent=base.id, base=base.id, artifact="gone.gguf")
    ModelLines.set_current_version(line.id, base.id)

    def _exists(repo, ref):
        return ref != "gone.gguf"

    with pytest.raises(VersionUnresolvable) as excinfo:
        resolve_version(stale.id, artifact_exists=_exists)

    message = str(excinfo.value)
    assert stale.id in message
    assert stale.corpus_commit_id in message
    assert "gone.gguf" in message
    assert base.id not in message, "resolution fell back to the line's current version"


def test_a_version_recording_no_artifact_is_unresolvable(db_session, app_state, commits):
    line = _line()
    version = _append(app_state, line.id, artifact=None)

    with pytest.raises(VersionUnresolvable, match="records no artifact"):
        resolve_version(version.id)


def test_an_adapter_whose_base_artifact_is_gone_is_unresolvable(db_session, app_state, commits):
    """Both halves must be present; the adapter alone is not servable."""
    line = _line()
    base = _append(app_state, line.id, artifact="gemma-q4.gguf")
    adapter = _append(app_state, line.id, kind="adapter", parent=base.id, base=base.id, artifact="ada.gguf")

    with pytest.raises(VersionUnresolvable) as excinfo:
        resolve_version(adapter.id, artifact_exists=lambda repo, ref: ref != "gemma-q4.gguf")

    assert base.id in str(excinfo.value)


def test_resolving_a_missing_version_is_reported(db_session):
    with pytest.raises(VersionUnresolvable, match="not found"):
        resolve_version("ghost")


####################
# R8 — revert rebuilds nothing
####################


def test_revert_moves_the_pointer_without_committing(db_session, app_state, commits):
    line = _line()
    first = _append(app_state, line.id)
    second = _append(app_state, line.id, kind="adapter", parent=first.id, base=first.id)
    ModelLines.set_current_version(line.id, second.id)
    commits_before = len(commits)
    versions_before = [v.id for v in ModelVersions.get_versions_by_line(line.id)]

    reverted = revert_line(line.id, first.id, moved_by="u1")

    assert reverted.current_version_id == first.id
    assert len(commits) == commits_before, "a revert must not commit"
    assert [v.id for v in ModelVersions.get_versions_by_line(line.id)] == versions_before


def test_revert_to_a_foreign_version_is_refused(db_session, app_state, commits):
    line_a, line_b = _line(name="a"), _line(corpus_repo="selfai-line-b", name="b")
    foreign = _append(app_state, line_a.id)

    with pytest.raises(ModelVersionError, match="does not belong"):
        revert_line(line_b.id, foreign.id)


####################
# The table layer's own guard still holds under the service
####################


def test_insert_still_refuses_a_version_with_no_commit(db_session):
    line = _line()
    with pytest.raises(ModelVersionError, match="commit"):
        ModelVersions.insert_new_version(line.id, ModelVersionForm(kind="base", corpus_commit_id=""))
