"""Model weights in self.corpus — self.ai#141.

Three things are actually being asserted here, and they are the three the issue
says matter:

- **Atomicity.** A failed or interrupted ingest must leave nothing visible on
  the default branch. Most of this file is failure cases, because "it works
  when everything works" is the cheap half.
- **Streaming.** Weights are multi-GB. The pump is driven for real so a change
  that starts buffering shows up as a test failure rather than as an OOM in
  production.
- **The catalogue link in both directions**, including the ways it breaks.

Async cases use `asyncio.run(...)` from a sync test — this repo registers no
pytest-asyncio and runs under `--strict-markers`, so `@pytest.mark.asyncio`
would error (same posture as test_self_corpus_primitives.py).
"""

import asyncio
import json
import re
from types import SimpleNamespace

import pytest

from selfai_ui.models.models import ModelForm, ModelMeta, ModelParams, Models
from selfai_ui.utils import model_weights, self_corpus
from selfai_ui.utils.model_weights import (
    CATALOGUE_OBJECT_PATH,
    CORPUS_COMMIT_FIELD,
    CORPUS_REPO_FIELD,
    CORPUS_WEIGHTS_PRESENT_FIELD,
    WEIGHTS_PREFIX,
    WeightsIngestError,
    WeightsProvenance,
    backfill_missing_model_repos,
    check_catalogue_links,
    download_to_repo,
    hf_resolve_url,
    hf_source_from_model_row,
    open_hf_object,
    read_corpus_weights,
)
from selfai_ui.utils.self_corpus import (
    SELFAI_REPO_PREFIX,
    SelfCorpusError,
    is_model_weights_repo,
    repo_id_for_kb,
    repo_id_for_model,
    repo_id_for_model_line,
)
from tests.mocks.external_services import aioresponses_strict

ENDPOINT = "http://terminus.test"
KEY = "AKIATEST"
SECRET = "secrettest"
MODEL_ID = "Qwen2.5-Coder-32B-Instruct-Q4_K_M.gguf"
HF_REPO = "bartowski/Qwen2.5-Coder-32B-Instruct-GGUF"

# LakeFS validates repository names against exactly this.
LAKEFS_REPO_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,62}$")


@pytest.fixture
def corpus_enabled(monkeypatch):
    """ENABLE_SELF_CORPUS is a PersistentConfig; the guard reads .value."""
    monkeypatch.setattr("selfai_ui.config.ENABLE_SELF_CORPUS", SimpleNamespace(value=True), raising=False)


@pytest.fixture
def corpus_disabled(monkeypatch):
    monkeypatch.setattr("selfai_ui.config.ENABLE_SELF_CORPUS", SimpleNamespace(value=False), raising=False)


def _app_state(enabled: bool = True):
    return SimpleNamespace(
        config=SimpleNamespace(
            ENABLE_SELF_CORPUS=enabled,
            SELF_CORPUS_LAKEFS_ENDPOINT=ENDPOINT,
            SELF_CORPUS_LAKEFS_ACCESS_KEY_ID=KEY,
            SELF_CORPUS_LAKEFS_SECRET_ACCESS_KEY=SECRET,
        )
    )


def _model_row(model_id=MODEL_ID, meta=None):
    return Models.insert_new_model(
        ModelForm(
            id=model_id,
            name=model_id,
            meta=ModelMeta(**(meta or {})),
            params=ModelParams(),
        ),
        "u1",
    )


####################
# A repo per model — the prefix is IAM, not convention
####################


@pytest.mark.tier0
def test_model_weights_repo_carries_the_granted_prefix():
    """Without it, create/delete 401s whatever the credential."""
    assert repo_id_for_model(MODEL_ID).startswith(SELFAI_REPO_PREFIX)


@pytest.mark.tier0
def test_all_three_repo_kinds_derive_from_the_shared_prefix_constant(monkeypatch):
    """One edit must move all of them, for the day the ACL grant is widened."""
    monkeypatch.setattr(self_corpus, "SELFAI_REPO_PREFIX", "widened-")
    assert repo_id_for_kb("abc").startswith("widened-")
    assert repo_id_for_model_line("abc").startswith("widened-")
    assert repo_id_for_model("abc").startswith("widened-")


@pytest.mark.tier0
@pytest.mark.parametrize(
    "model_id",
    [
        MODEL_ID,
        "a",
        "___",
        "UPPER.Case_Model",
        "x" * 400,
        "gemma-4-26B-A4B-Q8_0.gguf",
    ],
)
def test_repo_id_is_always_a_name_lakefs_will_accept(model_id):
    """Repo names are ^[a-z0-9][a-z0-9-]{2,62}$ — a model id is a GGUF filename."""
    assert LAKEFS_REPO_RE.match(repo_id_for_model(model_id)), repo_id_for_model(model_id)


@pytest.mark.tier0
def test_ids_that_slugify_identically_still_get_different_repos():
    """Two models sharing one weights repo is data loss, not a naming wart."""
    a = repo_id_for_model("Qwen2.5-Coder-32B")
    b = repo_id_for_model("Qwen2_5_Coder_32B")
    assert a != b


@pytest.mark.tier0
def test_repo_id_is_stable_across_calls():
    assert repo_id_for_model(MODEL_ID) == repo_id_for_model(MODEL_ID)


@pytest.mark.tier0
def test_weights_repos_are_distinguishable_from_line_and_kb_repos():
    assert is_model_weights_repo(repo_id_for_model("abc"))
    assert not is_model_weights_repo(repo_id_for_model_line("abc"))
    assert not is_model_weights_repo(repo_id_for_kb("abc"))


####################
# HF source — the URL is built from untrusted input
####################


@pytest.mark.tier0
def test_hf_resolve_url_is_the_direct_download_url():
    assert hf_resolve_url(HF_REPO, "model.gguf") == f"https://huggingface.co/{HF_REPO}/resolve/main/model.gguf"


@pytest.mark.tier0
@pytest.mark.security
@pytest.mark.parametrize(
    "hf_repo,filename",
    [
        ("../../etc", "passwd"),
        ("evil.com/x@attacker.test", "model.gguf"),
        ("owner/repo", "../../../etc/passwd"),
        ("owner/repo", "sub/../../escape.gguf"),
        ("", "model.gguf"),
        ("owner/repo", ""),
    ],
)
def test_hf_resolve_url_refuses_traversal_and_host_rewriting(hf_repo, filename):
    with pytest.raises(ValueError):
        hf_resolve_url(hf_repo, filename)


####################
# Streaming — the pump is driven for real
####################


@pytest.mark.tier0
def test_open_hf_object_yields_chunks_and_never_reads_the_body_whole(corpus_enabled):
    """`response.read()` is the thing this path exists to avoid."""
    payload = b"x" * (3 * 1024) + b"y" * 17
    url = hf_resolve_url(HF_REPO, "model.gguf")

    async def _drive():
        seen = []
        async with open_hf_object(HF_REPO, "model.gguf", chunk_size=1024) as (described, chunks):
            async for chunk in chunks:
                seen.append(chunk)
        return described, seen

    with aioresponses_strict() as m:
        m.get(url, body=payload, headers={"Content-Length": str(len(payload)), "X-Repo-Commit": "deadbeef"})
        described, seen = asyncio.run(_drive())

    assert b"".join(seen) == payload
    assert len(seen) > 1, "the body arrived as one piece; that is a buffer, not a stream"
    assert max(len(c) for c in seen) <= 1024
    assert described.size == len(payload)
    assert described.hf_commit == "deadbeef"


@pytest.mark.tier0
def test_open_hf_object_raises_on_an_http_error(corpus_enabled):
    with aioresponses_strict() as m:
        m.get(hf_resolve_url(HF_REPO, "model.gguf"), status=404, body="not found")

        async def _drive():
            async with open_hf_object(HF_REPO, "model.gguf"):
                pass  # pragma: no cover — the context manager raises first

        with pytest.raises(WeightsIngestError):
            asyncio.run(_drive())


@pytest.mark.tier0
def test_upload_object_pulls_its_body_from_an_async_iterator(corpus_enabled):
    """Pull-based, chunk at a time — the body is never handed over whole.

    (aioresponses joins the chunks itself before recording the request, so the
    assertion that matters is on the generator's side: it was driven to
    exhaustion one chunk at a time rather than materialised by the caller.)
    """
    pulled = []

    async def _chunks():
        for chunk in (b"ab", b"cd", b"ef"):
            pulled.append(chunk)
            yield chunk

    repo = repo_id_for_model(MODEL_ID)
    with aioresponses_strict() as m:
        m.put(re.compile(rf"{ENDPOINT}/api/v1/repositories/{repo}/branches/wip/objects.*"), payload={}, status=201)
        asyncio.run(self_corpus.upload_object(ENDPOINT, KEY, SECRET, repo, "wip", "weights/a.gguf", _chunks()))
        request = next(iter(m.requests.values()))[0]

    assert pulled == [b"ab", b"cd", b"ef"]
    assert bytes(request.kwargs["data"]) == b"abcdef"


@pytest.mark.tier0
def test_upload_object_is_typed_to_refuse_a_whole_body(corpus_enabled):
    """The durable guard: a signature taking bytes makes a 20 GiB model a 20 GiB buffer.

    Asserting the annotation rather than the behaviour on purpose — nothing
    observable distinguishes "streamed" from "buffered then streamed" at the
    HTTP boundary, but a change of this parameter to `bytes` is exactly the
    change that would introduce the buffer, and it is visible here.
    """
    import typing

    hints = typing.get_type_hints(self_corpus.upload_object)
    assert hints["stream"] == typing.AsyncIterator[bytes]


####################
# Ingest — orchestration and atomicity
#
# LakeFS branch/commit/merge/delete go through aioresponses for real; the
# object PUT is replaced with a fake that *drains* the stream, so the HF ->
# counter -> upload path is exercised end to end minus the final socket. That
# is the only way to assert bytes actually flowed: aioresponses records the
# request body without consuming an async iterator.
####################


class _RecordingUpload:
    """A stand-in for upload_object that really drains what it is given."""

    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    async def __call__(
        self, endpoint, key, secret, repo_id, branch, path, stream, content_type="application/octet-stream"
    ):
        body = bytearray()
        async for chunk in stream:
            body += chunk
        if self.fail_on and self.fail_on in path:
            raise SelfCorpusError(f"upload of {path} failed")
        self.calls.append({"repo": repo_id, "branch": branch, "path": path, "body": bytes(body)})
        return {}


def _lakefs_mocks(m, repo, *, branch_status=201, commit_status=201, merge_status=200):
    m.post(f"{ENDPOINT}/api/v1/repositories", payload={"id": repo}, status=201)
    m.post(f"{ENDPOINT}/api/v1/repositories/{repo}/branches", payload={"id": "ingest"}, status=branch_status)
    m.post(
        re.compile(rf"{ENDPOINT}/api/v1/repositories/{repo}/branches/ingest-[0-9a-f]+/commits$"),
        payload={"id": "c0ffee"},
        status=commit_status,
    )
    m.post(
        re.compile(rf"{ENDPOINT}/api/v1/repositories/{repo}/refs/ingest-[0-9a-f]+/merge/main$"),
        payload={"reference": "merged0"},
        status=merge_status,
    )
    m.delete(
        re.compile(rf"{ENDPOINT}/api/v1/repositories/{repo}/branches/ingest-[0-9a-f]+$"),
        status=204,
        repeat=True,
    )


def _methods_and_paths(m):
    return [(method, url.path) for method, url in m.requests.keys()]


@pytest.mark.tier0
def test_ingest_commits_on_an_ingest_branch_and_merges_into_main(db_session, corpus_enabled, monkeypatch):
    repo = repo_id_for_model(MODEL_ID)
    upload = _RecordingUpload()
    monkeypatch.setattr(model_weights, "upload_object", upload)
    _model_row()

    with aioresponses_strict() as m:
        m.get(hf_resolve_url(HF_REPO, MODEL_ID), body=b"w" * 4096, headers={"X-Repo-Commit": "abc123"})
        _lakefs_mocks(m, repo)
        result = asyncio.run(
            download_to_repo(
                _app_state(),
                model_id=MODEL_ID,
                hf_repo=HF_REPO,
                filenames=[MODEL_ID],
            )
        )
        paths = _methods_and_paths(m)

    assert result.corpus_repo == repo
    assert result.corpus_commit_id == "merged0"
    assert result.total_bytes == 4096
    assert [o.filename for o in result.objects] == [MODEL_ID]

    # Nothing was ever committed directly onto main.
    assert not any(p.endswith("/branches/main/commits") for _, p in paths)
    # Weights went to the ingest branch, not main.
    assert {c["branch"] for c in upload.calls} == {result_branch(upload)}
    assert upload.calls[0]["path"] == f"{WEIGHTS_PREFIX}/{MODEL_ID}"
    assert upload.calls[0]["body"] == b"w" * 4096


def result_branch(upload):
    branch = upload.calls[0]["branch"]
    assert branch.startswith("ingest-"), branch
    return branch


@pytest.mark.tier0
def test_a_failed_upload_leaves_nothing_on_main_and_drops_the_branch(db_session, corpus_enabled, monkeypatch):
    """The worst outcome here is a half-uploaded model appearing as a model."""
    repo = repo_id_for_model(MODEL_ID)
    upload = _RecordingUpload(fail_on="shard-2")
    monkeypatch.setattr(model_weights, "upload_object", upload)
    _model_row()

    with aioresponses_strict() as m:
        m.get(hf_resolve_url(HF_REPO, "shard-1"), body=b"a" * 32)
        m.get(hf_resolve_url(HF_REPO, "shard-2"), body=b"b" * 32)
        _lakefs_mocks(m, repo)
        with pytest.raises(WeightsIngestError):
            asyncio.run(
                download_to_repo(
                    _app_state(),
                    model_id=MODEL_ID,
                    hf_repo=HF_REPO,
                    filenames=["shard-1", "shard-2"],
                )
            )
        methods = _methods_and_paths(m)

    assert not any("/merge/" in p for _, p in methods), "a failed ingest must not merge"
    assert not any(p.endswith("/commits") for _, p in methods), "a failed ingest must not commit"
    assert any(method == "DELETE" and "/branches/ingest-" in p for method, p in methods), "the branch was not dropped"
    # And the row never learned about weights that are not there.
    assert read_corpus_weights(Models.get_model_by_id(MODEL_ID).meta.model_dump()) is None


@pytest.mark.tier0
def test_a_failed_commit_never_merges_and_drops_the_branch(db_session, corpus_enabled, monkeypatch):
    repo = repo_id_for_model(MODEL_ID)
    monkeypatch.setattr(model_weights, "upload_object", _RecordingUpload())
    _model_row()

    with aioresponses_strict() as m:
        m.get(hf_resolve_url(HF_REPO, MODEL_ID), body=b"w" * 16)
        _lakefs_mocks(m, repo, commit_status=500)
        with pytest.raises(WeightsIngestError):
            asyncio.run(
                download_to_repo(_app_state(), model_id=MODEL_ID, hf_repo=HF_REPO, filenames=[MODEL_ID])
            )
        methods = _methods_and_paths(m)

    assert not any("/merge/" in p for _, p in methods)
    assert any(method == "DELETE" and "/branches/ingest-" in p for method, p in methods)
    assert read_corpus_weights(Models.get_model_by_id(MODEL_ID).meta.model_dump()) is None


@pytest.mark.tier0
def test_a_failed_hf_download_drops_the_branch(db_session, corpus_enabled, monkeypatch):
    repo = repo_id_for_model(MODEL_ID)
    monkeypatch.setattr(model_weights, "upload_object", _RecordingUpload())
    _model_row()

    with aioresponses_strict() as m:
        m.get(hf_resolve_url(HF_REPO, MODEL_ID), status=403, body="gated repo")
        _lakefs_mocks(m, repo)
        with pytest.raises(WeightsIngestError):
            asyncio.run(
                download_to_repo(_app_state(), model_id=MODEL_ID, hf_repo=HF_REPO, filenames=[MODEL_ID])
            )
        methods = _methods_and_paths(m)

    assert not any("/merge/" in p for _, p in methods)
    assert any(method == "DELETE" and "/branches/ingest-" in p for method, p in methods)


@pytest.mark.tier0
def test_ingest_refuses_when_self_corpus_is_switched_off(db_session):
    with pytest.raises(WeightsIngestError):
        asyncio.run(
            download_to_repo(_app_state(enabled=False), model_id=MODEL_ID, hf_repo=HF_REPO, filenames=[MODEL_ID])
        )


@pytest.mark.tier0
def test_ingest_refuses_an_empty_file_list(db_session, corpus_enabled):
    with pytest.raises(WeightsIngestError):
        asyncio.run(download_to_repo(_app_state(), model_id=MODEL_ID, hf_repo=HF_REPO, filenames=[]))


####################
# Commit provenance — lineage readable from the corpus side
####################


@pytest.mark.tier0
def test_the_commit_records_what_produced_the_weights(db_session, corpus_enabled, monkeypatch):
    repo = repo_id_for_model(MODEL_ID)
    monkeypatch.setattr(model_weights, "upload_object", _RecordingUpload())
    _model_row()
    provenance = WeightsProvenance(
        kind="distillation",
        source_model="gemma-4-26B-A4B",
        source_corpus_repo="selfai-transcripts",
        source_corpus_commit="c0ffee",
        recipe={"prune": "depth", "blocks_dropped": 12},
        job_id="job-7",
    )

    with aioresponses_strict() as m:
        m.get(hf_resolve_url(HF_REPO, MODEL_ID), body=b"w" * 8, headers={"X-Repo-Commit": "hfsha"})
        _lakefs_mocks(m, repo)
        asyncio.run(
            download_to_repo(
                _app_state(),
                model_id=MODEL_ID,
                hf_repo=HF_REPO,
                filenames=[MODEL_ID],
                provenance=provenance,
            )
        )
        commit = next(
            reqs[0] for (method, url), reqs in m.requests.items() if method == "POST" and url.path.endswith("/commits")
        )

    metadata = commit.kwargs["json"]["metadata"]
    assert metadata["selfai.kind"] == "distillation"
    assert metadata["selfai.source_model"] == "gemma-4-26B-A4B"
    assert metadata["selfai.source_corpus_commit"] == "c0ffee"
    assert metadata["selfai.hf_repo"] == HF_REPO
    assert metadata["selfai.hf_commit"] == "hfsha", "the resolved sha, not the ref, is what makes it reproducible"
    assert json.loads(metadata["selfai.recipe"]) == {"prune": "depth", "blocks_dropped": 12}
    assert metadata["selfai.model_id"] == MODEL_ID
    # LakeFS commit metadata is a string->string map.
    assert all(isinstance(v, str) for v in metadata.values())


@pytest.mark.tier0
def test_provenance_metadata_is_all_strings():
    metadata = WeightsProvenance(kind="quantization", recipe={"quant": "Q4_K_M"}).as_commit_metadata()
    assert metadata["selfai.kind"] == "quantization"
    assert json.loads(metadata["selfai.recipe"]) == {"quant": "Q4_K_M"}
    assert all(isinstance(v, str) for v in metadata.values())


####################
# The catalogue link, both directions
####################


@pytest.mark.tier0
def test_the_repo_carries_enough_to_identify_the_model_row(db_session, corpus_enabled, monkeypatch):
    repo = repo_id_for_model(MODEL_ID)
    upload = _RecordingUpload()
    monkeypatch.setattr(model_weights, "upload_object", upload)
    _model_row()

    with aioresponses_strict() as m:
        m.get(hf_resolve_url(HF_REPO, MODEL_ID), body=b"w" * 8)
        _lakefs_mocks(m, repo)
        asyncio.run(download_to_repo(_app_state(), model_id=MODEL_ID, hf_repo=HF_REPO, filenames=[MODEL_ID]))

    catalogue = next(c for c in upload.calls if c["path"] == CATALOGUE_OBJECT_PATH)
    document = json.loads(catalogue["body"])
    assert document["model_id"] == MODEL_ID
    assert document["corpus_repo"] == repo
    assert document["provenance"]["hf_repo"] == HF_REPO


@pytest.mark.tier0
def test_the_row_references_its_repo_and_commit(db_session, corpus_enabled, monkeypatch):
    repo = repo_id_for_model(MODEL_ID)
    monkeypatch.setattr(model_weights, "upload_object", _RecordingUpload())
    _model_row()

    with aioresponses_strict() as m:
        m.get(hf_resolve_url(HF_REPO, MODEL_ID), body=b"w" * 8)
        _lakefs_mocks(m, repo)
        asyncio.run(download_to_repo(_app_state(), model_id=MODEL_ID, hf_repo=HF_REPO, filenames=[MODEL_ID]))

    pointer = read_corpus_weights(Models.get_model_by_id(MODEL_ID).meta.model_dump())
    assert pointer["repo"] == repo
    assert pointer["commit_id"] == "merged0"
    assert pointer["weights_present"] is True
    # Byte totals, object counts and the producing job stay on the corpus side
    # only. Two copies that can disagree is worse than one lookup.
    meta = Models.get_model_by_id(MODEL_ID).meta.model_dump()
    assert "total_bytes" not in meta and "object_count" not in meta


@pytest.mark.tier0
def test_a_row_pointing_at_a_missing_repo_is_detectable(db_session, corpus_enabled):
    _model_row(meta={CORPUS_REPO_FIELD: "selfai-model-gone-00000000", CORPUS_COMMIT_FIELD: "c1"})

    with aioresponses_strict() as m:
        m.get(f"{ENDPOINT}/api/v1/repositories/selfai-model-gone-00000000", status=404)
        m.get(re.compile(rf"{ENDPOINT}/api/v1/repositories\?.*"), payload={"results": [], "pagination": {}})
        report = asyncio.run(check_catalogue_links(_app_state()))

    assert report["dangling_rows"] == [{"model_id": MODEL_ID, "repo": "selfai-model-gone-00000000"}]
    assert report["unlinked_repos"] == []
    assert report["unreachable"] == []


@pytest.mark.tier0
def test_a_weights_repo_no_row_points_at_is_detectable(db_session, corpus_enabled):
    """The merged-but-not-yet-linked gap, and the deleted-row case."""
    orphan = repo_id_for_model("some-model-nobody-has.gguf")

    with aioresponses_strict() as m:
        m.get(
            re.compile(rf"{ENDPOINT}/api/v1/repositories\?.*"),
            payload={
                "results": [{"id": orphan}, {"id": repo_id_for_kb("kb-1")}, {"id": repo_id_for_model_line("line-x")}],
                "pagination": {},
            },
        )
        report = asyncio.run(check_catalogue_links(_app_state()))

    assert report["unlinked_repos"] == [orphan], "KB and line repos are not model-weights repos"
    assert report["dangling_rows"] == []


@pytest.mark.tier0
def test_an_outage_is_reported_as_an_outage_not_as_orphans(db_session, corpus_enabled):
    """"self.corpus is down" must never present as "everything is orphaned"."""
    _model_row(meta={CORPUS_REPO_FIELD: "selfai-model-x-00000000", CORPUS_COMMIT_FIELD: "c1"})

    with aioresponses_strict() as m:
        m.get(f"{ENDPOINT}/api/v1/repositories/selfai-model-x-00000000", status=500, body="lakefs down")
        m.get(re.compile(rf"{ENDPOINT}/api/v1/repositories\?.*"), status=500, body="lakefs down")
        report = asyncio.run(check_catalogue_links(_app_state()))

    assert report["dangling_rows"] == []
    assert report["unlinked_repos"] == []
    assert len(report["unreachable"]) == 2


@pytest.mark.tier0
def test_catalogue_check_is_a_no_op_when_corpus_is_off(db_session):
    assert asyncio.run(check_catalogue_links(_app_state(enabled=False)))["skipped"] is True


@pytest.mark.tier0
def test_a_legacy_row_with_no_pointer_reads_as_none(db_session):
    _model_row(meta={"hf_repo": HF_REPO, "quant": "Q4_K_M"})
    assert read_corpus_weights(Models.get_model_by_id(MODEL_ID).meta.model_dump()) is None


@pytest.mark.tier0
def test_the_link_uses_the_names_140_declared():
    """Confirmed against self.ai#140 (!462), not guessed.

    Pinned as literals: a rename here without a matching rename there is the
    failure this whole coordination existed to avoid.
    """
    assert CORPUS_REPO_FIELD == "corpus_repo"
    assert CORPUS_COMMIT_FIELD == "corpus_commit_id"
    assert CORPUS_WEIGHTS_PRESENT_FIELD == "corpus_weights_present"


@pytest.mark.tier0
def test_the_link_fields_are_declared_not_extras():
    """All three link fields are declared on ModelMeta, not riding extras.

    The point of #140 is that a consumer cannot rely on a field nothing
    enforces, so "we write the right names" is only half of it — they have to
    be declared.

    This carried a `pytest.skip` guard while #140 (!462) was still open. That
    guard is gone deliberately: !462 merged as e4b88ac, so the condition can no
    longer legitimately fire, and a guard that cannot fire turns a regression
    into a silent skip instead of a failure. A test that skips when the thing
    it protects breaks is the same green-badge-over-nothing problem #140 exists
    to end.
    """
    from selfai_ui.models.models import ModelMeta

    declared = set(ModelMeta.model_fields)
    missing = {
        CORPUS_REPO_FIELD,
        CORPUS_COMMIT_FIELD,
        CORPUS_WEIGHTS_PRESENT_FIELD,
    } - declared
    assert not missing, f"corpus link fields are not declared on ModelMeta: {sorted(missing)}"


@pytest.mark.tier0
def test_weights_repo_is_never_confused_with_a_distillation_source_corpus(db_session, corpus_enabled, monkeypatch):
    """`corpus_repo` is where the weights live; `source_corpus_repo` is the text.

    #140 names collapsing these as the thing that would make provenance a lie.
    A distillation's source corpus belongs in the commit metadata and the
    catalogue object, and must never reach the row's `corpus_repo`.
    """
    repo = repo_id_for_model(MODEL_ID)
    monkeypatch.setattr(model_weights, "upload_object", _RecordingUpload())
    _model_row()

    with aioresponses_strict() as m:
        m.get(hf_resolve_url(HF_REPO, MODEL_ID), body=b"w" * 8)
        _lakefs_mocks(m, repo)
        asyncio.run(
            download_to_repo(
                _app_state(),
                model_id=MODEL_ID,
                hf_repo=HF_REPO,
                filenames=[MODEL_ID],
                provenance=WeightsProvenance(
                    kind="distillation",
                    source_corpus_repo="selfai-transcripts",
                    source_corpus_commit="corpuscommit",
                ),
            )
        )

    meta = Models.get_model_by_id(MODEL_ID).meta.model_dump()
    assert meta[CORPUS_REPO_FIELD] == repo
    assert meta[CORPUS_REPO_FIELD] != "selfai-transcripts"
    assert meta[CORPUS_COMMIT_FIELD] == "merged0"
    assert meta[CORPUS_COMMIT_FIELD] != "corpuscommit"


@pytest.mark.tier0
def test_weights_present_distinguishes_a_linked_repo_from_one_holding_bytes(db_session):
    """The state the two declared fields cannot express — the #140 request."""
    _model_row(
        meta={
            CORPUS_REPO_FIELD: "selfai-model-x-00000000",
            CORPUS_COMMIT_FIELD: "c1",
            CORPUS_WEIGHTS_PRESENT_FIELD: False,
        }
    )
    pointer = read_corpus_weights(Models.get_model_by_id(MODEL_ID).meta.model_dump())
    assert pointer["repo"] == "selfai-model-x-00000000"
    assert pointer["weights_present"] is False, "a real commit id must not imply the bytes are there"


@pytest.mark.tier0
def test_a_repo_with_no_commit_yet_is_still_a_link(db_session):
    """Keyed off corpus_repo: a null commit is an intermediate state, not absence."""
    _model_row(meta={CORPUS_REPO_FIELD: "selfai-model-x-00000000"})
    pointer = read_corpus_weights(Models.get_model_by_id(MODEL_ID).meta.model_dump())
    assert pointer["repo"] == "selfai-model-x-00000000"
    assert pointer["commit_id"] is None


####################
# The seam onto the existing HF pull path
####################


@pytest.mark.tier0
def test_the_source_is_read_off_the_pull_lineage_register_model_wrote(db_session):
    _model_row(meta={"hf_repo": HF_REPO, "quant": "Q4_K_M", "source_type": "gguf"})
    assert hf_source_from_model_row(MODEL_ID) == (HF_REPO, [MODEL_ID])


@pytest.mark.tier0
def test_a_row_with_no_pull_lineage_yields_no_guessed_source(db_session):
    _model_row(meta={"description": "hand-made"})
    assert hf_source_from_model_row(MODEL_ID) == (None, [])
    assert hf_source_from_model_row("no-such-model") == (None, [])


####################
# Backfill — degrade, do not break
####################


@pytest.mark.tier0
def test_backfill_links_an_existing_model_without_claiming_it_holds_weights(db_session, corpus_enabled, monkeypatch):
    """Core has no access to /models, so the repo must not pretend otherwise."""
    repo = repo_id_for_model(MODEL_ID)
    monkeypatch.setattr(model_weights, "upload_object", _RecordingUpload())
    _model_row(meta={"hf_repo": HF_REPO, "quant": "Q4_K_M"})

    with aioresponses_strict() as m:
        m.post(f"{ENDPOINT}/api/v1/repositories", payload={"id": repo}, status=201)
        m.post(f"{ENDPOINT}/api/v1/repositories/{repo}/branches/main/commits", payload={"id": "bf1"}, status=201)
        report = asyncio.run(backfill_missing_model_repos(_app_state()))

    assert report["created"] == [MODEL_ID]
    assert report["failed"] == []
    pointer = read_corpus_weights(Models.get_model_by_id(MODEL_ID).meta.model_dump())
    assert pointer["repo"] == repo
    assert pointer["commit_id"] == "bf1", "the catalogue commit is real; only the weights are absent"
    assert pointer["weights_present"] is False


@pytest.mark.tier0
def test_backfill_skips_rows_that_are_already_linked(db_session, corpus_enabled):
    _model_row(meta={CORPUS_REPO_FIELD: "selfai-model-already-00000000", CORPUS_COMMIT_FIELD: "c1"})
    report = asyncio.run(backfill_missing_model_repos(_app_state()))
    assert report == {"created": [], "failed": [], "already_linked": 1}


@pytest.mark.tier0
def test_one_backfill_failure_does_not_stop_the_rest(db_session, corpus_enabled, monkeypatch):
    """backfill_missing_repos' posture: log and continue, never fail the sweep."""
    monkeypatch.setattr(model_weights, "upload_object", _RecordingUpload())
    _model_row("broken.gguf")
    _model_row("fine.gguf")
    broken, fine = repo_id_for_model("broken.gguf"), repo_id_for_model("fine.gguf")

    with aioresponses_strict() as m:
        m.post(f"{ENDPOINT}/api/v1/repositories", payload={"id": broken}, status=201)
        m.get(f"{ENDPOINT}/api/v1/repositories/{broken}", status=404)
        m.post(f"{ENDPOINT}/api/v1/repositories/{broken}/branches/main/commits", status=500, body="nope")
        m.post(f"{ENDPOINT}/api/v1/repositories", payload={"id": fine}, status=201)
        m.post(f"{ENDPOINT}/api/v1/repositories/{fine}/branches/main/commits", payload={"id": "bf2"}, status=201)
        report = asyncio.run(backfill_missing_model_repos(_app_state()))

    assert report["failed"] == ["broken.gguf"]
    assert report["created"] == ["fine.gguf"]
    # The failed model still serves: its row is untouched and carries no pointer.
    assert read_corpus_weights(Models.get_model_by_id("broken.gguf").meta.model_dump()) is None


@pytest.mark.tier0
def test_backfill_is_a_no_op_when_corpus_is_off(db_session):
    assert asyncio.run(backfill_missing_model_repos(_app_state(enabled=False)))["skipped"] is True


@pytest.mark.tier0
def test_an_existing_repo_is_not_a_backfill_failure(db_session, corpus_enabled, monkeypatch):
    """Re-running the backfill must not fail on every already-created repo."""
    repo = repo_id_for_model(MODEL_ID)
    monkeypatch.setattr(model_weights, "upload_object", _RecordingUpload())
    _model_row()

    with aioresponses_strict() as m:
        m.post(f"{ENDPOINT}/api/v1/repositories", payload={"message": "already exists"}, status=409)
        m.get(f"{ENDPOINT}/api/v1/repositories/{repo}", payload={"id": repo}, status=200)
        m.post(f"{ENDPOINT}/api/v1/repositories/{repo}/branches/main/commits", payload={"id": "bf3"}, status=201)
        report = asyncio.run(backfill_missing_model_repos(_app_state()))

    assert report["created"] == [MODEL_ID]
