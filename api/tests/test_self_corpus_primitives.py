"""Strict self.corpus versioning primitives + model-line repo naming.

self.ai#131 — cavekit-model-versioning.md R3 (commit/branch/tag/merge that
raise) and R4 (model-line repo ids under the granted selfai-* ACL).

The point of R3 is the *failure* behaviour, so most of this file is failure
cases: the KB path must keep swallowing, and the versioning path must not.

Async cases use `asyncio.run(...)` from a sync test — this repo registers no
pytest-asyncio and runs under `--strict-markers`, so `@pytest.mark.asyncio`
would error (same posture as test_vram_device_occupancy.py).
"""

import asyncio
import sys
from types import SimpleNamespace

import pytest

from selfai_ui.utils import self_corpus
from selfai_ui.utils.self_corpus import (
    SELFAI_REPO_PREFIX,
    SelfCorpusError,
    SelfCorpusNothingToCommit,
    commit_branch,
    create_branch,
    create_tag,
    merge_into,
    repo_id_for_kb,
    repo_id_for_model_line,
)
from tests.mocks.external_services import aioresponses_strict

ENDPOINT = "http://terminus.test"
KEY = "AKIATEST"
SECRET = "secrettest"
REPO = "selfai-line-abc"


@pytest.fixture
def corpus_enabled(monkeypatch):
    """ENABLE_SELF_CORPUS is a PersistentConfig; the guard reads .value."""
    monkeypatch.setattr("selfai_ui.config.ENABLE_SELF_CORPUS", SimpleNamespace(value=True), raising=False)


@pytest.fixture
def corpus_disabled(monkeypatch):
    monkeypatch.setattr("selfai_ui.config.ENABLE_SELF_CORPUS", SimpleNamespace(value=False), raising=False)


####################
# R4 — repo naming
####################


def test_model_line_repo_carries_the_granted_prefix():
    assert repo_id_for_model_line("abc").startswith(SELFAI_REPO_PREFIX)


def test_model_line_and_kb_repos_are_distinguishable_for_the_same_id():
    line = repo_id_for_model_line("abc")
    kb = repo_id_for_kb("abc")
    assert line != kb
    assert self_corpus.MODEL_LINE_INFIX in line
    assert self_corpus.MODEL_LINE_INFIX not in kb


def test_both_repo_kinds_derive_from_the_shared_prefix_constant(monkeypatch):
    """One edit must move both, for the day the ACL grant is widened."""
    monkeypatch.setattr(self_corpus, "SELFAI_REPO_PREFIX", "widened-")
    assert repo_id_for_kb("abc").startswith("widened-")
    assert repo_id_for_model_line("abc").startswith("widened-")


####################
# R3 — success paths
####################


def test_commit_returns_the_commit_id(corpus_enabled):
    with aioresponses_strict() as m:
        m.post(
            f"{ENDPOINT}/api/v1/repositories/{REPO}/branches/main/commits",
            payload={"id": "c0ffee"},
            status=201,
        )
        commit_id = asyncio.run(commit_branch(ENDPOINT, KEY, SECRET, REPO, "publish v2"))
    assert commit_id == "c0ffee"


def test_commit_metadata_is_stringified(corpus_enabled):
    """LakeFS commit metadata is a string->string map; ints must not go raw."""
    with aioresponses_strict() as m:
        m.post(
            f"{ENDPOINT}/api/v1/repositories/{REPO}/branches/main/commits",
            payload={"id": "c0ffee"},
            status=201,
        )
        asyncio.run(commit_branch(ENDPOINT, KEY, SECRET, REPO, "bake", metadata={"sequence": 4}))
        request = next(iter(m.requests.values()))[0]
    assert request.kwargs["json"]["metadata"] == {"sequence": "4"}


def test_create_branch_posts_name_and_source(corpus_enabled):
    with aioresponses_strict() as m:
        m.post(f"{ENDPOINT}/api/v1/repositories/{REPO}/branches", payload={"id": "wip"}, status=201)
        result = asyncio.run(create_branch(ENDPOINT, KEY, SECRET, REPO, "wip", source_reference="main"))
        request = next(iter(m.requests.values()))[0]
    assert result == "wip"
    assert request.kwargs["json"] == {"name": "wip", "source": "main"}


def test_create_tag_posts_id_and_ref(corpus_enabled):
    with aioresponses_strict() as m:
        m.post(f"{ENDPOINT}/api/v1/repositories/{REPO}/tags", payload={"id": "v3"}, status=201)
        result = asyncio.run(create_tag(ENDPOINT, KEY, SECRET, REPO, "v3", "c0ffee"))
        request = next(iter(m.requests.values()))[0]
    assert result == "v3"
    assert request.kwargs["json"] == {"id": "v3", "ref": "c0ffee"}


def test_merge_returns_the_merge_reference(corpus_enabled):
    with aioresponses_strict() as m:
        m.post(
            f"{ENDPOINT}/api/v1/repositories/{REPO}/refs/wip/merge/main",
            payload={"reference": "merge0"},
            status=200,
        )
        result = asyncio.run(merge_into(ENDPOINT, KEY, SECRET, REPO, "wip", "main", message="publish"))
    assert result == "merge0"


####################
# R3 — failure paths
####################


@pytest.mark.parametrize("status", [401, 404, 500])
def test_commit_raises_on_error_status(corpus_enabled, status):
    with aioresponses_strict() as m:
        m.post(
            f"{ENDPOINT}/api/v1/repositories/{REPO}/branches/main/commits",
            payload={"message": "nope"},
            status=status,
        )
        with pytest.raises(SelfCorpusError) as excinfo:
            asyncio.run(commit_branch(ENDPOINT, KEY, SECRET, REPO, "publish"))
    assert not isinstance(excinfo.value, SelfCorpusNothingToCommit)
    assert str(status) in str(excinfo.value)


def test_empty_commit_is_distinguishable_from_a_rejection(corpus_enabled):
    """A publish that staged no bytes must not read like one LakeFS refused."""
    with aioresponses_strict() as m:
        m.post(
            f"{ENDPOINT}/api/v1/repositories/{REPO}/branches/main/commits",
            payload={"message": "commit: no changes"},
            status=400,
        )
        with pytest.raises(SelfCorpusNothingToCommit):
            asyncio.run(commit_branch(ENDPOINT, KEY, SECRET, REPO, "publish"))


def test_an_unrecognised_400_is_not_reported_as_a_no_op(corpus_enabled):
    with aioresponses_strict() as m:
        m.post(
            f"{ENDPOINT}/api/v1/repositories/{REPO}/branches/main/commits",
            payload={"message": "branch is protected"},
            status=400,
        )
        with pytest.raises(SelfCorpusError) as excinfo:
            asyncio.run(commit_branch(ENDPOINT, KEY, SECRET, REPO, "publish"))
    assert not isinstance(excinfo.value, SelfCorpusNothingToCommit)


def test_commit_without_an_id_raises(corpus_enabled):
    """A 201 with no id would otherwise produce a version backed by None."""
    with aioresponses_strict() as m:
        m.post(
            f"{ENDPOINT}/api/v1/repositories/{REPO}/branches/main/commits",
            payload={},
            status=201,
        )
        with pytest.raises(SelfCorpusError):
            asyncio.run(commit_branch(ENDPOINT, KEY, SECRET, REPO, "publish"))


@pytest.mark.parametrize("status", [401, 409])
def test_branch_tag_and_merge_raise_on_error_status(corpus_enabled, status):
    for path, call in (
        (f"/api/v1/repositories/{REPO}/branches", lambda: create_branch(ENDPOINT, KEY, SECRET, REPO, "wip")),
        (f"/api/v1/repositories/{REPO}/tags", lambda: create_tag(ENDPOINT, KEY, SECRET, REPO, "v3", "c0ffee")),
        (
            f"/api/v1/repositories/{REPO}/refs/wip/merge/main",
            lambda: merge_into(ENDPOINT, KEY, SECRET, REPO, "wip", "main"),
        ),
    ):
        with aioresponses_strict() as m:
            m.post(f"{ENDPOINT}{path}", payload={"message": "nope"}, status=status)
            with pytest.raises(SelfCorpusError):
                asyncio.run(call())


####################
# R3 — refusal before the wire
####################


def test_operations_refuse_when_the_flag_is_off(corpus_disabled):
    """No request is issued at all — no aioresponses mock is registered here,
    so an attempted call would surface as a connection error, not a pass."""
    for call in (
        lambda: commit_branch(ENDPOINT, KEY, SECRET, REPO, "publish"),
        lambda: create_branch(ENDPOINT, KEY, SECRET, REPO, "wip"),
        lambda: create_tag(ENDPOINT, KEY, SECRET, REPO, "v3", "c0ffee"),
        lambda: merge_into(ENDPOINT, KEY, SECRET, REPO, "wip", "main"),
    ):
        with pytest.raises(SelfCorpusError, match="disabled"):
            asyncio.run(call())


@pytest.mark.parametrize(
    "endpoint,key,secret",
    [("", KEY, SECRET), (ENDPOINT, "", SECRET), (ENDPOINT, KEY, "")],
)
def test_operations_refuse_when_credentials_are_unset(corpus_enabled, endpoint, key, secret):
    with pytest.raises(SelfCorpusError, match="not configured"):
        asyncio.run(commit_branch(endpoint, key, secret, REPO, "publish"))


####################
# R3 — the KB path must NOT have gained this behaviour
####################


def test_kb_commit_still_swallows_a_failing_corpus(monkeypatch):
    """storage/provider.py's best-effort posture is load-bearing for KBs.

    A corpus outage must not break the Knowledge Base feature, so the upload
    still returns normally even when the commit underneath it blows up.
    """
    from selfai_ui.storage.provider import StorageProvider

    class _Exploding:
        def __getattr__(self, name):
            raise RuntimeError("self.corpus is on fire")

    monkeypatch.setitem(sys.modules, "lakefs", _Exploding())
    monkeypatch.setitem(sys.modules, "lakefs.client", _Exploding())

    uploaded = []
    provider = object.__new__(StorageProvider)
    provider.corpus_client = SimpleNamespace(upload_file=lambda *a, **k: uploaded.append(a))

    provider._upload_to_corpus("/tmp/whatever", "kb1", "notes.txt")

    assert uploaded, "the object write itself should still have happened"
